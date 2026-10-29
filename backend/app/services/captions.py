from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import Settings
from backend.app.db.models import (
    CaptionTrack,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    ProductionJob,
    VideoRender,
)
from backend.app.media.captions import (
    CAPTION_PRESETS,
    DEFAULT_CAPTION_PRESET,
    SOURCE_LABELS,
    CaptionCue,
    CaptionGeneration,
    CaptionSettings,
    generate_from_chunks,
    generate_from_script,
    generate_from_transcription,
    list_caption_presets,
    normalise_caption_text,
    parse_subtitles,
    preset_settings,
    resolve_font_family,
    to_ass,
    to_srt,
    to_vtt,
    validate_cues,
    validate_settings,
)
from backend.app.media.transcription import (
    TranscriptionUnavailable,
    transcribe,
    transcription_status,
)

logger = logging.getLogger(__name__)


class CaptionError(ValueError):
    """A caption request cannot be fulfilled as specified."""


class CaptionNotFound(CaptionError):
    pass


class CaptionConflict(CaptionError):
    pass


class CaptionRegenerationRequired(CaptionConflict):
    """Manual edits exist and the narration changed underneath them."""


@dataclass(frozen=True)
class CaptionAssets:
    srt_path: Path
    vtt_path: Path
    ass_paths: dict[str, Path]

    def to_dict(self) -> dict[str, Any]:
        return {
            "srt_path": str(self.srt_path),
            "vtt_path": str(self.vtt_path),
            "ass_paths": {key: str(value) for key, value in self.ass_paths.items()},
        }


class CaptionService:
    """Generate, validate, persist, and export editable caption tracks."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        transcriber: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._transcriber = transcriber or transcribe

    # ------------------------------------------------------------------ lookups

    async def get_track(self, session: AsyncSession, track_id: str) -> CaptionTrack:
        track = await session.get(CaptionTrack, track_id)
        if track is None:
            raise CaptionNotFound("Caption track not found")
        return track

    async def get_for_script(self, session: AsyncSession, script_id: str) -> CaptionTrack | None:
        return await session.scalar(select(CaptionTrack).where(CaptionTrack.script_id == script_id))

    async def get_for_narration(
        self, session: AsyncSession, narration_id: str
    ) -> CaptionTrack | None:
        return await session.scalar(
            select(CaptionTrack).where(CaptionTrack.narration_audio_id == narration_id)
        )

    async def get_for_job(
        self, session: AsyncSession, job_id: str
    ) -> tuple[ProductionJob, NarrationAudio | None, CaptionTrack | None]:
        job = await session.get(ProductionJob, job_id)
        if job is None:
            raise CaptionNotFound("Production job not found")
        narration = None
        if job.script_id:
            narration = await session.scalar(
                select(NarrationAudio).where(NarrationAudio.script_id == job.script_id)
            )
        track = None
        if narration is not None:
            track = await self.get_for_narration(session, narration.id)
        if track is None and job.script_id:
            track = await self.get_for_script(session, job.script_id)
        return job, narration, track

    # --------------------------------------------------------------- generation

    async def generate_for_script(
        self,
        session: AsyncSession,
        script_id: str,
        *,
        narration_id: str | None = None,
        preset_key: str | None = None,
        settings: CaptionSettings | dict[str, Any] | None = None,
        force: bool = False,
        allow_script_fallback: bool = True,
    ) -> CaptionTrack:
        script = await session.get(NewsScript, script_id)
        if script is None:
            raise CaptionNotFound("Script not found")
        narration = (
            await session.get(NarrationAudio, narration_id)
            if narration_id
            else await session.scalar(
                select(NarrationAudio).where(NarrationAudio.script_id == script.id)
            )
        )
        if narration is not None and narration.script_id != script.id:
            raise CaptionConflict("The narration does not belong to this script.")
        chunks = (
            list(
                (
                    await session.scalars(
                        select(NarrationChunk)
                        .where(NarrationChunk.narration_audio_id == narration.id)
                        .order_by(NarrationChunk.position.asc())
                    )
                ).all()
            )
            if narration is not None
            else []
        )
        if narration is None and not allow_script_fallback:
            raise CaptionError(
                "Narration is not ready. Generate the voiceover before creating captions."
            )
        duration = _narration_duration(narration, chunks, script)
        fingerprint = _audio_fingerprint(narration, chunks, duration)
        track = await self.get_for_script(session, script.id)
        existing_settings = (
            CaptionSettings.from_dict(track.settings_json, preset_settings(track.preset_key))
            if track is not None and track.settings_json
            else None
        )
        selected_preset = (
            preset_key
            or (track.preset_key if track is not None else None)
            or self._settings.caption_default_preset
            or DEFAULT_CAPTION_PRESET
        )
        if selected_preset not in CAPTION_PRESETS:
            raise CaptionError(f"Unknown caption preset: {selected_preset}")
        if settings is None:
            # Reuse customisations only while the caller stays on the preset the
            # settings were built for. An explicit preset switch must apply that
            # preset's defaults instead of silently keeping the previous look.
            if existing_settings is not None and (
                preset_key is None or existing_settings.preset_key == preset_key
            ):
                base = existing_settings
            else:
                base = preset_settings(selected_preset)
        elif isinstance(settings, CaptionSettings):
            base = settings
        else:
            base = CaptionSettings.from_dict(settings, preset_settings(selected_preset))
        if base.preset_key != selected_preset:
            base = replace(base, preset_key=selected_preset)

        if track is not None and not force:
            same_narration = narration is not None and track.narration_audio_id == narration.id
            same_timing = not track.audio_fingerprint or track.audio_fingerprint == fingerprint
            if same_narration and same_timing and track.cues_json:
                # A different caption preset is an explicit presentation change:
                # regenerate, but never discard a manually edited track.
                if track.preset_key == selected_preset or track.edited:
                    return track
            if track.edited and track.cues_json:
                track.stale = True
                track.stale_reason = (
                    "The narration or its timing changed after these captions were edited. "
                    "Regenerate captions to confirm replacing the manual edits."
                )
                await session.commit()
                raise CaptionRegenerationRequired(
                    "The narration changed after captions were edited. Open the caption "
                    "editor and choose Regenerate to confirm replacing the manual edits, "
                    "then re-render the video."
                )

        if track is None:
            track = CaptionTrack(
                script_id=script.id,
                narration_audio_id=narration.id if narration else None,
                status="generating",
                preset_key=selected_preset,
                language="en",
                source="narration_alignment",
                source_detail="",
                settings_json=base.to_dict(),
                cues_json=[],
                checks_json=[],
                warnings=[],
                edited=False,
                stale=False,
                revision=1,
                audio_fingerprint=fingerprint,
                ass_paths={},
            )
            session.add(track)
        else:
            track.status = "generating"
            track.narration_audio_id = narration.id if narration else track.narration_audio_id
            track.preset_key = selected_preset
            track.settings_json = base.to_dict()
            track.audio_fingerprint = fingerprint
            track.stale = False
            track.stale_reason = None
        await session.flush()
        await session.commit()

        warnings: list[str] = []
        generation = await self._generate(
            script,
            narration,
            chunks,
            base,
            duration,
            warnings,
        )
        if not generation.cues:
            track.status = "failed"
            track.warnings = [*warnings, *generation.warnings]
            track.checks_json = [
                {
                    "name": "cues_present",
                    "status": "failed",
                    "detail": "Caption generation produced no cues.",
                }
            ]
            await session.commit()
            raise CaptionError(
                "Caption generation produced no usable cues. Check the narration audio "
                "and transcription settings, then retry."
            )

        family, font_warnings = resolve_font_family(
            base.font_family,
            language=generation.language,
            fonts_dir=self._settings.caption_fonts_dir,
        )
        base = replace(base, font_family=family)
        warnings.extend(font_warnings)
        warnings.extend(generation.warnings)
        checks = self._validate(
            generation.cues,
            base,
            duration,
            generation.language,
            family,
        )
        failed = [check for check in checks if check["status"] == "failed"]
        track.language = generation.language
        track.source = generation.source
        track.source_detail = generation.source_detail
        track.settings_json = base.to_dict()
        track.cues_json = [cue.to_dict() for cue in generation.cues]
        track.checks_json = checks
        track.warnings = _dedupe([*warnings, *_check_warnings(checks)])
        track.edited = False
        track.stale = False
        track.stale_reason = None
        track.revision = max(1, int(track.revision or 1)) + 1
        if failed:
            track.status = "failed"
            await session.commit()
            raise CaptionError(
                "Caption validation failed: "
                + "; ".join(f"{check['name']}: {check['detail']}" for check in failed)
            )
        assets = await self.write_assets(track)
        track.status = "ready"
        track.srt_path = str(assets.srt_path)
        track.vtt_path = str(assets.vtt_path)
        track.ass_paths = {key: str(value) for key, value in assets.ass_paths.items()}
        await session.commit()
        await session.refresh(track)
        return track

    async def ensure_for_narration(
        self,
        session: AsyncSession,
        narration: NarrationAudio,
        *,
        preset_key: str | None = None,
        settings: CaptionSettings | dict[str, Any] | None = None,
        force: bool = False,
    ) -> CaptionTrack:
        return await self.generate_for_script(
            session,
            narration.script_id,
            narration_id=narration.id,
            preset_key=preset_key,
            settings=settings,
            force=force,
            allow_script_fallback=False,
        )

    async def _generate(
        self,
        script: NewsScript,
        narration: NarrationAudio | None,
        chunks: list[NarrationChunk],
        settings: CaptionSettings,
        duration: float,
        warnings: list[str],
    ) -> CaptionGeneration:
        provider = (self._settings.caption_transcription_provider or "auto").lower()
        if narration is not None and provider not in {"none", "off", "disabled"}:
            try:
                result = self._transcriber(
                    Path(narration.wav_path),
                    provider=provider,
                    model_size=self._settings.caption_whisper_model,
                    language=self._settings.caption_language,
                )
                if result.segments:
                    generation = generate_from_transcription(
                        result.segments,
                        settings,
                        duration=duration,
                        language=None
                        if self._settings.caption_language in {None, "", "auto"}
                        else self._settings.caption_language,
                        source_detail=result.detail,
                    )
                    if generation.cues:
                        warnings.extend(result.warnings)
                        return generation
                    warnings.append(
                        "Speech recognition returned no usable cues; narration timing "
                        "is used instead."
                    )
                else:
                    warnings.append(
                        "Speech recognition returned no segments; narration timing is used instead."
                    )
            except TranscriptionUnavailable as exc:
                warnings.append(f"{exc} Narration-aligned captions are used instead.")
            except Exception as exc:  # noqa: BLE001 - optional local backend
                logger.warning("Caption transcription failed: %s", exc)
                warnings.append(
                    f"Speech recognition failed ({exc}). Narration-aligned captions "
                    "are used instead."
                )
        if chunks:
            generation = generate_from_chunks(
                chunks,
                settings,
                duration=duration,
                source="narration_alignment",
                source_detail=(
                    "Timing is derived from the measured TTS chunk durations and the "
                    "narration text. It is not verified by speech recognition."
                ),
            )
            warnings.extend(generation.warnings)
            return generation
        if script.full_text:
            generation = generate_from_script(
                script.full_text,
                settings,
                duration=duration,
            )
            warnings.extend(generation.warnings)
            return generation
        return CaptionGeneration(
            cues=[],
            source="script_fallback",
            source_detail=SOURCE_LABELS["script_fallback"],
            language="en",
            warnings=["The script and narration contain no caption text."],
        )

    def _validate(
        self,
        cues: list[CaptionCue],
        settings: CaptionSettings,
        duration: float,
        language: str,
        font_family: str,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for aspect, width, height in (
            ("16:9", self._settings.video_landscape_width, self._settings.video_landscape_height),
            ("9:16", self._settings.video_portrait_width, self._settings.video_portrait_height),
        ):
            for check in validate_settings(
                settings, width=width, height=height, aspect_ratio=aspect
            ):
                checks.append(
                    {
                        "name": f"{aspect}:{check.name}",
                        "status": check.status,
                        "detail": check.detail,
                    }
                )
            for check in validate_cues(
                cues,
                duration=duration,
                settings=settings,
                aspect_ratio=aspect,
                language=language,
                font_family=font_family,
            ):
                checks.append(
                    {
                        "name": f"{aspect}:{check.name}",
                        "status": check.status,
                        "detail": check.detail,
                    }
                )
        return checks

    # ------------------------------------------------------------------- editing

    async def update_settings(
        self,
        session: AsyncSession,
        track_id: str,
        *,
        preset_key: str | None = None,
        settings: CaptionSettings | dict[str, Any] | None = None,
    ) -> CaptionTrack:
        track = await self.get_track(session, track_id)
        if track.status == "rendering":
            raise CaptionConflict("Captions are rendering; wait for the render to finish.")
        selected = preset_key or track.preset_key or DEFAULT_CAPTION_PRESET
        if selected not in CAPTION_PRESETS:
            raise CaptionError(f"Unknown caption preset: {selected}")
        if settings is None:
            base = preset_settings(selected)
        elif isinstance(settings, CaptionSettings):
            base = settings
        else:
            base = CaptionSettings.from_dict(settings, preset_settings(selected))
        base = replace(base, preset_key=selected)
        cues = [CaptionCue.from_dict(item) for item in (track.cues_json or [])]
        duration = await self._duration_for_track(session, track, cues)
        family, font_warnings = resolve_font_family(
            base.font_family,
            language=track.language or "en",
            fonts_dir=self._settings.caption_fonts_dir,
        )
        base = replace(base, font_family=family)
        checks = self._validate(cues, base, duration, track.language or "en", family)
        failed = [check for check in checks if check["status"] == "failed"]
        if failed:
            raise CaptionError(
                "Caption settings are not valid for this video: "
                + "; ".join(f"{check['name']}: {check['detail']}" for check in failed)
            )
        track.preset_key = selected
        track.settings_json = base.to_dict()
        track.checks_json = checks
        track.warnings = _dedupe(
            [*(track.warnings or []), *font_warnings, *_check_warnings(checks)]
        )
        if cues:
            track.status = "edited"
            track.edited = True
        assets = await self.write_assets(track)
        track.srt_path = str(assets.srt_path)
        track.vtt_path = str(assets.vtt_path)
        track.ass_paths = {key: str(value) for key, value in assets.ass_paths.items()}
        await session.commit()
        await session.refresh(track)
        return track

    async def update_cues(
        self,
        session: AsyncSession,
        track_id: str,
        cues: list[CaptionCue] | list[dict[str, Any]],
        *,
        settings: CaptionSettings | dict[str, Any] | None = None,
    ) -> CaptionTrack:
        track = await self.get_track(session, track_id)
        if track.status == "rendering":
            raise CaptionConflict("Captions are rendering; wait for the render to finish.")
        parsed = [cue if isinstance(cue, CaptionCue) else CaptionCue.from_dict(cue) for cue in cues]
        parsed = _normalise_cues(parsed)
        if not parsed:
            raise CaptionError("A caption track must contain at least one cue.")
        base = CaptionSettings.from_dict(track.settings_json, preset_settings(track.preset_key))
        if settings is not None:
            base = (
                settings
                if isinstance(settings, CaptionSettings)
                else CaptionSettings.from_dict(settings, base)
            )
        duration = await self._duration_for_track(session, track, parsed)
        family, font_warnings = resolve_font_family(
            base.font_family,
            language=track.language or "en",
            fonts_dir=self._settings.caption_fonts_dir,
        )
        base = replace(base, font_family=family)
        checks = self._validate(parsed, base, duration, track.language or "en", family)
        failed = [check for check in checks if check["status"] == "failed"]
        if failed:
            raise CaptionError(
                "Caption edits are not valid: "
                + "; ".join(f"{check['name']}: {check['detail']}" for check in failed)
            )
        track.settings_json = base.to_dict()
        track.cues_json = [cue.to_dict() for cue in parsed]
        track.checks_json = checks
        track.warnings = _dedupe(
            [*(track.warnings or []), *font_warnings, *_check_warnings(checks)]
        )
        track.status = "edited"
        track.edited = True
        track.revision = max(1, int(track.revision or 1)) + 1
        assets = await self.write_assets(track)
        track.srt_path = str(assets.srt_path)
        track.vtt_path = str(assets.vtt_path)
        track.ass_paths = {key: str(value) for key, value in assets.ass_paths.items()}
        await session.commit()
        await session.refresh(track)
        return track

    async def import_subtitles(
        self,
        session: AsyncSession,
        track_id: str,
        *,
        content: str,
        format: str,
        settings: CaptionSettings | dict[str, Any] | None = None,
    ) -> CaptionTrack:
        cues = parse_subtitles(content, format)
        if not cues:
            raise CaptionError(
                "No readable cues were found in the file. Check the SRT or WebVTT structure."
            )
        track = await self.get_track(session, track_id)
        track.source = "imported"
        track.source_detail = f"Imported {format.upper()} subtitle file"
        updated = await self.update_cues(session, track_id, cues, settings=settings)
        updated.source = "imported"
        updated.source_detail = f"Imported {format.upper()} subtitle file"
        await session.commit()
        await session.refresh(updated)
        return updated

    async def regenerate(
        self,
        session: AsyncSession,
        track_id: str,
        *,
        force: bool = False,
        preset_key: str | None = None,
        settings: CaptionSettings | dict[str, Any] | None = None,
    ) -> CaptionTrack:
        track = await self.get_track(session, track_id)
        if track.status == "rendering":
            raise CaptionConflict("Captions are rendering; wait for the render to finish.")
        if track.edited and not force:
            raise CaptionRegenerationRequired(
                "This track has manual edits. Confirm regeneration to replace them, "
                "or keep the current captions."
            )
        return await self.generate_for_script(
            session,
            track.script_id,
            narration_id=track.narration_audio_id,
            preset_key=preset_key,
            settings=settings,
            force=True,
        )

    # ------------------------------------------------------------------ assets

    async def write_assets(
        self,
        track: CaptionTrack,
        *,
        output_dir: Path | None = None,
    ) -> CaptionAssets:
        settings = CaptionSettings.from_dict(track.settings_json, preset_settings(track.preset_key))
        cues = [CaptionCue.from_dict(item) for item in (track.cues_json or [])]
        directory = output_dir or (self._settings.caption_output_dir / track.id)
        directory.mkdir(parents=True, exist_ok=True)
        srt_path = directory / "captions.srt"
        vtt_path = directory / "captions.vtt"
        srt_path.write_text(to_srt(cues, settings), encoding="utf-8")
        vtt_path.write_text(
            to_vtt(cues, settings, source=SOURCE_LABELS.get(track.source, track.source)),
            encoding="utf-8",
        )
        ass_paths: dict[str, Path] = {}
        family, _ = resolve_font_family(
            settings.font_family,
            language=track.language or "en",
            fonts_dir=self._settings.caption_fonts_dir,
        )
        for aspect, width, height in (
            ("16:9", self._settings.video_landscape_width, self._settings.video_landscape_height),
            ("9:16", self._settings.video_portrait_width, self._settings.video_portrait_height),
        ):
            path = directory / f"captions-{aspect.replace(':', 'x')}.ass"
            path.write_text(
                to_ass(
                    cues,
                    settings,
                    aspect_ratio=aspect,
                    width=width,
                    height=height,
                    font_family=family,
                    language=track.language or "en",
                ),
                encoding="utf-8",
            )
            ass_paths[aspect] = path
        return CaptionAssets(srt_path=srt_path, vtt_path=vtt_path, ass_paths=ass_paths)

    async def copy_sidecars(self, track: CaptionTrack, output_dir: Path) -> CaptionAssets:
        assets = await self.write_assets(track, output_dir=output_dir)
        return assets

    async def mark_rendering(self, session: AsyncSession, track_id: str) -> None:
        track = await self.get_track(session, track_id)
        track.status = "rendering"
        await session.commit()

    async def mark_completed(self, session: AsyncSession, track_id: str) -> None:
        track = await self.get_track(session, track_id)
        track.status = "completed"
        await session.commit()

    async def mark_failed(self, session: AsyncSession, track_id: str, error: str) -> None:
        track = await self.get_track(session, track_id)
        track.status = "failed"
        track.warnings = _dedupe([*(track.warnings or []), error])
        await session.commit()

    async def mark_stale_for_narration(
        self, session: AsyncSession, narration_id: str, reason: str
    ) -> CaptionTrack | None:
        track = await self.get_for_narration(session, narration_id)
        if track is None:
            return None
        track.stale = True
        track.stale_reason = reason
        if track.edited:
            track.warnings = _dedupe([*(track.warnings or []), reason])
        await session.commit()
        await session.refresh(track)
        return track

    # ----------------------------------------------------------------- helpers

    async def _duration_for_track(
        self, session: AsyncSession, track: CaptionTrack, cues: list[CaptionCue]
    ) -> float:
        narration = (
            await session.get(NarrationAudio, track.narration_audio_id)
            if track.narration_audio_id
            else None
        )
        if narration is not None and narration.duration_seconds:
            return float(narration.duration_seconds)
        if cues:
            return max(cue.end for cue in cues) + 0.5
        return 60.0

    def serialize_track(
        self, track: CaptionTrack, *, duration_seconds: float | None = None
    ) -> dict[str, Any]:
        settings = CaptionSettings.from_dict(track.settings_json, preset_settings(track.preset_key))
        cues = [CaptionCue.from_dict(item) for item in (track.cues_json or [])]
        return {
            "id": track.id,
            "script_id": track.script_id,
            "narration_audio_id": track.narration_audio_id,
            "status": track.status,
            "preset_key": track.preset_key,
            "language": track.language,
            "source": track.source,
            "source_detail": track.source_detail,
            "source_label": SOURCE_LABELS.get(track.source, track.source),
            "settings": settings.to_dict(),
            "cues": [cue.to_dict() for cue in cues],
            "checks": track.checks_json or [],
            "warnings": track.warnings or [],
            "edited": bool(track.edited),
            "stale": bool(track.stale),
            "stale_reason": track.stale_reason,
            "revision": track.revision,
            "audio_fingerprint": track.audio_fingerprint,
            "srt_url": f"/api/v1/captions/tracks/{track.id}/export/srt",
            "vtt_url": f"/api/v1/captions/tracks/{track.id}/export/vtt",
            "ass_urls": {
                aspect: f"/api/v1/captions/tracks/{track.id}/export/ass?aspect={aspect}"
                for aspect in ("16:9", "9:16")
            },
            "duration_seconds": duration_seconds
            or (max((cue.end for cue in cues), default=0.0) or None),
            "created_at": track.created_at,
            "updated_at": track.updated_at,
        }

    def build_config(self) -> dict[str, Any]:
        return {
            "presets": list_caption_presets(),
            "defaults": {
                "preset_key": self._settings.caption_default_preset,
                "burn_in": self._settings.caption_burn_in_default,
                "soft_subs": self._settings.caption_soft_subs_default,
                "sidecar": self._settings.caption_sidecar_default,
            },
            "fonts": _font_payload(self._settings),
            "statuses": list(
                (
                    "not_generated",
                    "generating",
                    "ready",
                    "edited",
                    "rendering",
                    "completed",
                    "failed",
                )
            ),
            "source_labels": SOURCE_LABELS,
            "transcription": transcription_status(),
            "burn_in_available": True,
            "soft_subs_available": True,
            "sidecar_available": True,
        }


def _narration_duration(
    narration: NarrationAudio | None,
    chunks: list[NarrationChunk],
    script: NewsScript,
) -> float:
    if narration is not None and narration.duration_seconds:
        return max(0.5, float(narration.duration_seconds))
    if chunks:
        return max(
            0.5,
            max(
                float(chunk.start_seconds or 0.0) + float(chunk.duration_seconds or 0.0)
                for chunk in chunks
            ),
        )
    return max(5.0, float(script.estimated_duration_seconds or 60))


def _audio_fingerprint(
    narration: NarrationAudio | None,
    chunks: list[NarrationChunk],
    duration: float,
) -> str:
    payload = {
        "narration": narration.id if narration else None,
        "duration": round(duration, 3),
        "chunks": [
            [
                normalise_caption_text(chunk.text or ""),
                round(float(chunk.start_seconds or 0.0), 3),
                round(float(chunk.duration_seconds or 0.0), 3),
            ]
            for chunk in chunks
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _words_match_text(words: list[Any], text: str) -> bool:
    """True when stored word timings still describe the cue text.

    Manual text edits leave the old word list behind. Rendering karaoke or
    emphasis against words that no longer match the text would highlight the
    wrong tokens, so the caller re-times the cue instead.
    """

    joined = " ".join(str(getattr(word, "text", "") or "") for word in words)
    return normalise_caption_text(joined) == normalise_caption_text(text)


def _normalise_cues(cues: list[CaptionCue]) -> list[CaptionCue]:
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    output: list[CaptionCue] = []
    for index, cue in enumerate(ordered, start=1):
        text = normalise_caption_text(cue.text)
        if not text:
            continue
        start = max(0.0, cue.start)
        end = max(start + 0.05, cue.end)
        words = cue.words
        if not words or not _words_match_text(words, text):
            from backend.app.media.captions import allocate_words

            words = allocate_words(
                text,
                start,
                end - start,
                emphasis=any(getattr(word, "emphasis", False) for word in words),
            )
        else:
            words = [
                replace(
                    word,
                    start=min(max(start, word.start), end),
                    end=min(max(start, word.end), end),
                )
                for word in words
            ]
        output.append(
            CaptionCue(
                index=index,
                start=round(start, 3),
                end=round(end, 3),
                text=text,
                words=words,
                speaker=cue.speaker,
            )
        )
    return output


def _check_warnings(checks: list[dict[str, Any]]) -> list[str]:
    return [
        f"{check['name']}: {check['detail']}"
        for check in checks
        if check.get("status") == "warning"
    ]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = " ".join(str(item).split())
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _font_payload(settings: Settings) -> list[dict[str, Any]]:
    from backend.app.media.captions import available_fonts

    return available_fonts(settings.caption_fonts_dir)


async def caption_track_for_render(
    service: CaptionService,
    session: AsyncSession,
    narration: NarrationAudio,
    *,
    preset_key: str | None,
    settings: CaptionSettings | dict[str, Any] | None,
    force: bool = False,
) -> CaptionTrack:
    """Resolve the track a renderer should use, generating one if necessary."""

    track = await service.get_for_narration(session, narration.id)
    if track is not None and not force and not track.stale and track.cues_json:
        return track
    return await service.ensure_for_narration(
        session,
        narration,
        preset_key=preset_key,
        settings=settings,
        force=force,
    )


async def caption_track_for_video_render(
    session: AsyncSession, render: VideoRender
) -> CaptionTrack | None:
    narration = await session.get(NarrationAudio, render.narration_audio_id)
    if narration is None:
        return None
    return await session.scalar(
        select(CaptionTrack).where(CaptionTrack.narration_audio_id == narration.id)
    )
