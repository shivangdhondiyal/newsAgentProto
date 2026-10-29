from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.errors import AgentRunFailed
from backend.app.agents.tts.audio import merge_with_pauses, wave_metadata
from backend.app.agents.tts.contracts import SpeechSynthesizer
from backend.app.agents.tts.providers import PiperSpeechSynthesizer
from backend.app.core.config import Settings
from backend.app.db.models import (
    AgentRun,
    AudioMaster,
    ContentApproval,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    PublishingRecord,
    Scene,
    ScenePlan,
    VideoRender,
    VideoVariant,
)
from backend.app.media.audio import master_narration, resolve_executable
from backend.app.schemas.tts import TTSRunRead


@dataclass(frozen=True)
class NarrationPart:
    text: str
    pause_ms: int
    paragraph_end: bool


class TTSAgent:
    """Chunks a script, creates local narration, and retains synchronization metadata."""

    name = "tts"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        synthesizer: SpeechSynthesizer | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._synthesizer = synthesizer or self._create_synthesizer(settings)

    async def execute(
        self,
        *,
        script_ids: list[str] | None = None,
        force: bool = False,
    ) -> TTSRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                candidates = await self._load_eligible_scripts(session, script_ids, force)
                run.items_seen = len(candidates)
                for script, scene_plan in candidates:
                    if force:
                        await self._clear_narration(session, script.id)
                    warnings = await self._create_narration(session, script, scene_plan)
                    run.items_created += 1
                    if warnings:
                        run.warnings = [*run.warnings, *warnings]
                run.status = "completed"
            except Exception as exc:
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise AgentRunFailed(self.name, run.id, run.errors[-1], run.errors)
        return TTSRunRead.model_validate(run)

    async def _load_eligible_scripts(
        self,
        session: AsyncSession,
        script_ids: list[str] | None = None,
        force: bool = False,
    ) -> list[tuple[NewsScript, ScenePlan]]:
        statement = (
            select(NewsScript, ScenePlan)
            .join(ScenePlan, ScenePlan.script_id == NewsScript.id)
            .outerjoin(NarrationAudio, NarrationAudio.script_id == NewsScript.id)
            .where(
                NewsScript.status == "draft",
                ScenePlan.status == "draft",
            )
            .order_by(NewsScript.created_at.asc())
            .limit(self._settings.tts_batch_size)
        )
        if script_ids:
            statement = statement.where(NewsScript.id.in_(script_ids))
        if not force:
            statement = statement.where(NarrationAudio.id.is_(None))
        return list((await session.execute(statement)).tuples().all())

    async def _clear_narration(self, session: AsyncSession, script_id: str) -> None:
        """Replace the previous narration for one script, invalidating stale renders.

        An approved or published render is never invalidated implicitly: the
        operator must withdraw it first. Draft renders are removed so the new
        narration cannot point at stale audio, and the append-only approval
        event log keeps the audit history.
        """

        narration = await session.scalar(
            select(NarrationAudio).where(NarrationAudio.script_id == script_id)
        )
        if narration is None:
            return
        render = await session.scalar(
            select(VideoRender).where(VideoRender.narration_audio_id == narration.id)
        )
        if render is not None:
            approval = await session.scalar(
                select(ContentApproval).where(ContentApproval.video_render_id == render.id)
            )
            if approval is not None and approval.status == "approved":
                raise ValueError(
                    "The existing render is approved; withdraw the approval before "
                    "re-narrating this script."
                )
            published = await session.scalar(
                select(PublishingRecord.id)
                .where(PublishingRecord.video_render_id == render.id)
                .limit(1)
            )
            if published is not None:
                raise ValueError(
                    "The existing render has a publishing record; it cannot be "
                    "invalidated by re-narration."
                )
            await session.execute(
                delete(ContentApproval).where(ContentApproval.video_render_id == render.id)
            )
            await session.execute(
                delete(VideoVariant).where(VideoVariant.video_render_id == render.id)
            )
            await session.delete(render)
            await session.flush()
        await session.execute(
            delete(NarrationChunk).where(NarrationChunk.narration_audio_id == narration.id)
        )
        await session.execute(
            delete(AudioMaster).where(AudioMaster.narration_audio_id == narration.id)
        )
        await session.delete(narration)
        await session.flush()

    async def _create_narration(
        self, session: AsyncSession, script: NewsScript, scene_plan: ScenePlan
    ) -> list[str]:
        warnings: list[str] = []
        parts = self._chunk_script(await self._narration_text(session, script, scene_plan))
        output_dir = self._settings.tts_output_dir / script.id
        chunk_paths: list[Path] = []
        chunk_metadata = []
        for position, part in enumerate(parts, start=1):
            path = output_dir / f"chunk-{position:03d}.wav"
            await self._synthesize_with_retry(part.text, path, warnings)
            chunk_paths.append(path)
            chunk_metadata.append(wave_metadata(path))
        raw_path = output_dir / "narration.wav"
        merged, starts = merge_with_pauses(chunk_paths, [part.pause_ms for part in parts], raw_path)
        mastered_path = raw_path
        master: AudioMaster | None = None
        if self._settings.audio_master_enabled:
            ffmpeg = resolve_executable(self._settings.tts_ffmpeg_executable)
            if ffmpeg is None:
                warnings.append(
                    "FFmpeg was not found; narration was left at its raw peak-normalized level."
                )
            else:
                mastered_path = (
                    self._settings.audio_output_dir / script.id / "narration-mastered.wav"
                )
                try:
                    validation = await master_narration(
                        ffmpeg,
                        raw_path,
                        mastered_path,
                        target_lufs=self._settings.audio_target_lufs,
                        true_peak=self._settings.audio_true_peak_db,
                        sample_rate=self._settings.audio_sample_rate,
                        channels=self._settings.audio_channels,
                        duration_seconds=merged.duration_seconds,
                    )
                    if not validation.passed:
                        warnings.append("Audio mastering produced failed validation checks.")
                    master = AudioMaster(
                        narration_audio_id="",
                        raw_wav_path=str(raw_path),
                        mastered_wav_path=str(mastered_path),
                        status="completed" if validation.passed else "warning",
                        duration_seconds=validation.duration_seconds or merged.duration_seconds,
                        sample_rate=validation.sample_rate or self._settings.audio_sample_rate,
                        channels=validation.channels or self._settings.audio_channels,
                        integrated_lufs=validation.integrated_lufs,
                        true_peak_dbtp=validation.true_peak_dbtp,
                        checks_json=validation.checks,
                    )
                except (RuntimeError, OSError) as exc:
                    warnings.append(f"Audio mastering failed; raw narration was kept: {exc}")
                    mastered_path = raw_path

        mastered_meta = wave_metadata(mastered_path)
        mp3_path = (
            await self._export_mp3(mastered_path) if self._settings.tts_generate_mp3 else None
        )
        narration = NarrationAudio(
            script_id=script.id,
            scene_plan_id=scene_plan.id,
            status="completed",
            provider_name=self._synthesizer.name,
            voice_name=self._synthesizer.voice_name,
            wav_path=str(mastered_path),
            mp3_path=str(mp3_path) if mp3_path else None,
            duration_seconds=round(mastered_meta.duration_seconds, 3),
            sample_rate=mastered_meta.sample_rate,
        )
        session.add(narration)
        await session.flush()
        if master is not None:
            master.narration_audio_id = narration.id
            session.add(master)
        for position, (part, path, metadata, start) in enumerate(
            zip(parts, chunk_paths, chunk_metadata, starts, strict=True), start=1
        ):
            session.add(
                NarrationChunk(
                    narration_audio_id=narration.id,
                    position=position,
                    text=part.text,
                    wav_path=str(path),
                    start_seconds=round(start, 3),
                    duration_seconds=round(metadata.duration_seconds, 3),
                )
            )
        return warnings

    async def _narration_text(
        self, session: AsyncSession, script: NewsScript, scene_plan: ScenePlan
    ) -> str:
        """Read the same cleaned copy that the scene plan will show on screen.

        Narrating ``script.full_text`` spoke production boilerplate that the
        planner had already removed from the scenes, so the voiceover and the
        captions disagreed with the visuals. The planned scene narration is the
        single source of truth for both.
        """

        scenes = (
            await session.scalars(
                select(Scene)
                .where(Scene.scene_plan_id == scene_plan.id)
                .order_by(Scene.position.asc())
            )
        ).all()
        planned = [" ".join((scene.narration or "").split()) for scene in scenes]
        text = "\n\n".join(segment for segment in planned if segment)
        return text or script.full_text

    async def _synthesize_with_retry(self, text: str, path: Path, warnings: list[str]) -> None:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                await self._synthesizer.synthesize(text, path)
                return
            except (RuntimeError, OSError) as exc:
                last_error = exc
                if attempt == 0:
                    warnings.append(f"Chunk retry after synthesis error: {exc}")
        assert last_error is not None
        raise last_error

    def _chunk_script(self, text: str) -> list[NarrationPart]:
        paragraphs = [
            " ".join(paragraph.split())
            for paragraph in re.split(r"\n\s*\n", text or "")
            if paragraph.strip()
        ]
        if not paragraphs:
            paragraphs = [" ".join((text or "").split())]
        parts: list[NarrationPart] = []
        for paragraph in paragraphs:
            paragraph_parts = self._chunk_paragraph(paragraph)
            if not paragraph_parts:
                continue
            for index, part in enumerate(paragraph_parts):
                is_last = index == len(paragraph_parts) - 1
                parts.append(
                    NarrationPart(
                        text=part,
                        pause_ms=(
                            self._settings.audio_paragraph_pause_ms
                            if is_last
                            else self._settings.audio_sentence_pause_ms
                        ),
                        paragraph_end=is_last,
                    )
                )
        if not parts:
            raise ValueError("Script contains no narration text")
        return parts

    def _chunk_paragraph(self, paragraph: str) -> list[str]:
        sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", paragraph) if item.strip()]
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if len(sentence) > self._settings.tts_chunk_max_characters:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(self._split_long_text(sentence))
            elif (
                current
                and len(current) + len(sentence) + 1 > self._settings.tts_chunk_max_characters
            ):
                chunks.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            chunks.append(current)
        return chunks

    def _split_long_text(self, text: str) -> list[str]:
        words = text.split()
        chunks: list[str] = []
        current = ""
        for word in words:
            if current and len(current) + len(word) + 1 > self._settings.tts_chunk_max_characters:
                chunks.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            chunks.append(current)
        return chunks

    async def _export_mp3(self, wav_path: Path) -> Path:
        mp3_path = wav_path.with_suffix(".mp3")
        process = await asyncio.create_subprocess_exec(
            self._settings.tts_ffmpeg_executable,
            "-y",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            str(mp3_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not mp3_path.is_file():
            raise RuntimeError(
                f"FFmpeg MP3 export failed: {stderr.decode('utf-8', errors='replace')}"
            )
        return mp3_path

    @staticmethod
    def _create_synthesizer(settings: Settings) -> SpeechSynthesizer:
        if settings.tts_provider == "piper":
            return PiperSpeechSynthesizer(
                settings.tts_piper_executable,
                settings.tts_piper_voice_model,
                length_scale=settings.tts_length_scale,
                noise_scale=settings.tts_noise_scale,
                noise_w=settings.tts_noise_w,
                sentence_silence=settings.tts_sentence_silence,
            )
        raise ValueError("Unsupported TTS provider; use 'piper'")
