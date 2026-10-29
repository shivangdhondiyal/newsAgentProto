from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.errors import AgentRunFailed
from backend.app.agents.video.contracts import VideoRenderer
from backend.app.agents.video.subtitles import render_srt
from backend.app.core.config import Settings
from backend.app.db.models import (
    AgentRun,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    PublishingRecord,
    Scene,
    SceneAsset,
    ScenePlan,
    VideoRender,
    VideoVariant,
    VisualAsset,
)
from backend.app.media.audio import mix_music, resolve_executable
from backend.app.media.captions import default_fonts_dir
from backend.app.media.craft import shot_for_position, transition_for_scene
from backend.app.media.genres import build_effective_profile
from backend.app.media.graphics import SceneVisual, choose_graphic_types, render_scene_frame
from backend.app.media.keywords import search_keywords
from backend.app.media.motion import (
    apply_saved_graphics,
    plan_graphics_for_timeline,
    render_scene_overlays,
)
from backend.app.media.presets import EditingPreset
from backend.app.media.renderer import CaptionRender, FFmpegSceneRenderer, SceneClip
from backend.app.media.script_text import headline_for_text
from backend.app.media.timeline import align_scenes_to_narration
from backend.app.reviews.service import get_or_create_approval
from backend.app.schemas.video import VideoRunRead
from backend.app.services.captions import CaptionService, caption_track_for_render

ASPECT_SIZES = {
    "16:9": ("video_landscape_width", "video_landscape_height"),
    "9:16": ("video_portrait_width", "video_portrait_height"),
}


class VideoGeneratorAgent:
    """Renders local draft videos; it has no publishing authority."""

    name = "video_generator"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        renderer: VideoRenderer | None = None,
        caption_service: CaptionService | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._caption_service = caption_service
        self._renderer = renderer or FFmpegSceneRenderer(
            settings.video_ffmpeg_executable,
            fps=settings.video_fps,
            timeout_seconds=settings.video_render_timeout_seconds,
            scratch_dir=settings.video_scratch_dir,
        )

    async def execute(
        self,
        *,
        style_key: str | None = None,
        aspects: list[str] | None = None,
        narration_id: str | None = None,
        music_path: Path | None = None,
        captions: bool = True,
        genre_key: str | None = None,
        genre_overrides: dict[str, Any] | None = None,
        audio_path: Path | None = None,
    ) -> VideoRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                candidates = await self._load_eligible_narrations(session, narration_id)
                run.items_seen = len(candidates)
                for narration, scene_plan in candidates:
                    if not Path(narration.wav_path).is_file():
                        if narration_id:
                            raise FileNotFoundError(
                                f"Narration audio not found: {narration.wav_path}"
                            )
                        # A stale narration row (file deleted, disk restored from
                        # backup) must not fail the whole scheduled batch.
                        run.warnings = [
                            *run.warnings,
                            (
                                f"Narration {narration.id} has no audio file on disk; "
                                "skipped. Re-run narration for that story."
                            ),
                        ]
                        continue
                    warnings = await self._render_video(
                        session,
                        narration,
                        scene_plan,
                        style_key=style_key,
                        aspects=aspects,
                        music_path=music_path,
                        captions=captions,
                        genre_key=genre_key,
                        genre_overrides=genre_overrides,
                        audio_path=audio_path,
                    )
                    run.items_created += 1
                    if warnings:
                        run.warnings = [*run.warnings, *warnings]
                run.status = "completed"
            except Exception as exc:
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1_000, 2)
                await session.commit()
                await session.refresh(run)
        if run.status == "failed":
            raise AgentRunFailed(self.name, run.id, run.errors[-1], run.errors)
        return VideoRunRead.model_validate(run)

    async def _load_eligible_narrations(
        self, session: AsyncSession, narration_id: str | None = None
    ) -> list[tuple[NarrationAudio, ScenePlan]]:
        statement = (
            select(NarrationAudio, ScenePlan)
            .join(ScenePlan, ScenePlan.id == NarrationAudio.scene_plan_id)
            .join(NewsScript, NewsScript.id == NarrationAudio.script_id)
            .where(
                NarrationAudio.status == "completed",
                ScenePlan.status == "draft",
                NewsScript.status == "draft",
            )
            .order_by(NarrationAudio.created_at.asc())
            .limit(self._settings.video_batch_size)
        )
        if narration_id:
            statement = statement.where(NarrationAudio.id == narration_id)
        else:
            statement = statement.outerjoin(
                VideoRender, VideoRender.narration_audio_id == NarrationAudio.id
            ).where(VideoRender.id.is_(None))
        return list((await session.execute(statement)).tuples().all())

    async def _render_video(
        self,
        session: AsyncSession,
        narration: NarrationAudio,
        scene_plan: ScenePlan,
        *,
        style_key: str | None,
        aspects: list[str] | None,
        music_path: Path | None = None,
        captions: bool = True,
        genre_key: str | None = None,
        genre_overrides: dict[str, Any] | None = None,
        audio_path: Path | None = None,
    ) -> list[str]:
        chunks = list(
            (
                await session.scalars(
                    select(NarrationChunk)
                    .where(NarrationChunk.narration_audio_id == narration.id)
                    .order_by(NarrationChunk.position.asc())
                )
            ).all()
        )
        scenes = list(
            (
                await session.scalars(
                    select(Scene)
                    .where(Scene.scene_plan_id == scene_plan.id)
                    .order_by(Scene.position)
                )
            ).all()
        )
        if not chunks:
            raise ValueError(f"Narration {narration.id} has no timing chunks")
        if not scenes:
            raise ValueError(f"Scene plan {scene_plan.id} has no scenes")
        existing = await session.scalar(
            select(VideoRender).where(VideoRender.narration_audio_id == narration.id)
        )
        if existing is not None:
            publishing = await session.scalar(
                select(PublishingRecord.id).where(PublishingRecord.video_render_id == existing.id)
            )
            if publishing:
                raise ValueError(
                    "This narration already has publishing activity; create a new story "
                    "instead of re-rendering the approved artifact"
                )

        script = await session.get(NewsScript, narration.script_id)
        story_text = " ".join(
            part
            for part in (
                script.title if script else "",
                script.full_text if script else "",
                " ".join(scene.narration for scene in scenes),
            )
            if part
        )
        profile = build_effective_profile(
            genre_key or "general_news",
            style_key=style_key or self._settings.video_default_style,
            story_text=story_text,
            overrides=genre_overrides,
        )
        preset = profile.preset
        timings = align_scenes_to_narration(scenes, chunks, narration.duration_seconds)
        scene_assets = await self._load_scene_assets(session, [scene.id for scene in scenes])
        output_dir = self._settings.video_output_dir / narration.id
        output_dir.mkdir(parents=True, exist_ok=True)
        asset_lookup: dict[int, tuple[bool, str]] = {}
        for scene in scenes:
            row = scene_assets.get(scene.id)
            if row is None:
                asset_lookup[scene.position] = (False, "graphic")
                continue
            scene_asset, asset = row
            ready = bool(
                scene_asset.status == "ready"
                and asset is not None
                and Path(asset.local_path).is_file()
            )
            kind = "graphic"
            if ready and asset is not None:
                suffix = Path(asset.local_path).suffix.lower()
                kind = (
                    "video"
                    if asset.media_type == "video" or suffix in {".mp4", ".mov", ".webm", ".mkv"}
                    else "photo"
                )
            asset_lookup[scene.position] = (ready, kind)
        requested = _normalize_aspects(aspects or self._settings.video_aspects)
        # Frame templates are chosen once for the whole run so the overlay
        # planner can complement them instead of re-adding a panel on top.
        frame_types = self._frame_types(scenes, timings, scene_assets, preset)
        plans_by_aspect = {
            aspect: plan_graphics_for_timeline(
                scenes=scenes,
                timings=timings,
                preset=preset,
                profile=profile,
                asset_lookup=asset_lookup,
                aspect_ratio=aspect,
                captions_enabled=captions,
                frame_types=frame_types,
            )
            for aspect in requested
        }
        # Restore operator edits from the persisted per-aspect plan. Automatic
        # planning refreshes everything else but never overwrites an edit.
        saved_by_scene = {
            scene.position: (scene.graphic_plan_json or {}).get("aspects") or {} for scene in scenes
        }
        for aspect, aspect_plans in plans_by_aspect.items():
            plans_by_aspect[aspect] = [
                apply_saved_graphics(
                    plan,
                    saved_by_scene.get(plan.scene_position, {}).get(aspect),
                )
                for plan in aspect_plans
            ]
        caption_track = None
        if captions and self._caption_service is not None:
            # The production pipeline generates captions in its own stage. A
            # direct video run resolves the persisted track (or creates one) so
            # the burned-in text and the soft track always come from one source.
            caption_track = await caption_track_for_render(
                self._caption_service,
                session,
                narration,
                preset_key=None,
                settings=None,
            )
        legacy_subtitle_path = output_dir / "subtitles.srt"
        legacy_subtitle_path.write_text(render_srt(chunks), encoding="utf-8")
        subtitle_path = (
            Path(caption_track.srt_path)
            if caption_track is not None and caption_track.srt_path
            else legacy_subtitle_path
        )
        timeline_path = output_dir / "timeline.json"
        timeline_path.write_text(
            json.dumps(
                [
                    {
                        "position": scene.position,
                        "start_seconds": round(timing.start_seconds, 3),
                        "duration_seconds": round(timing.duration_seconds, 3),
                        "narration": scene.narration,
                        "subtitle": scene.subtitle,
                        "keywords": scene.keywords,
                        "scene_description": scene.scene_description,
                        "stock_footage_query": scene.stock_footage_query,
                        "image_search_query": scene.image_search_query,
                        "animation_suggestion": scene.animation_suggestion,
                        "transition": scene.transition,
                        "style_key": preset.key,
                        "genre_key": profile.genre.key,
                        "editing_plan": next(
                            (
                                plan.to_dict()
                                for plan in plans_by_aspect.get("16:9", [])
                                if plan.scene_position == scene.position
                            ),
                            {},
                        ),
                    }
                    for scene, timing in zip(scenes, timings, strict=True)
                ],
                indent=2,
            ),
            encoding="utf-8",
        )

        warnings: list[str] = []
        for scene in scenes:
            row = scene_assets.get(scene.id)
            if row is None:
                continue
            scene_asset, asset = row
            if scene_asset.status == "ready" and (
                asset is None or not Path(asset.local_path).is_file()
            ):
                warnings.append(
                    f"Scene {scene.position}: the licensed asset file is missing; "
                    "a designed graphic is used instead."
                )
        render_audio_path = (
            audio_path
            if audio_path is not None and audio_path.is_file()
            else Path(narration.wav_path)
        )
        if (
            render_audio_path == Path(narration.wav_path)
            and music_path is not None
            and music_path.is_file()
        ):
            ffmpeg = resolve_executable(self._settings.video_ffmpeg_executable)
            if ffmpeg is None:
                warnings.append("FFmpeg was unavailable; licensed music was not mixed.")
            else:
                mixed_path = output_dir / "narration-with-music.wav"
                await mix_music(
                    ffmpeg,
                    Path(narration.wav_path),
                    music_path,
                    mixed_path,
                    duration_seconds=narration.duration_seconds,
                    music_gain=preset.craft.music_gain,
                    ducking=preset.craft.ducking,
                )
                render_audio_path = mixed_path
        if caption_track is not None:
            assert self._caption_service is not None
            await self._caption_service.mark_rendering(session, caption_track.id)
        try:
            rendered, render_warnings = await self._render_aspects(
                requested=requested,
                output_dir=output_dir,
                scenes=scenes,
                timings=timings,
                scene_assets=scene_assets,
                preset=preset,
                audio_path=render_audio_path,
                subtitle_path=subtitle_path,
                narration=narration,
                burn_scene_captions=captions and caption_track is None,
                caption_track=caption_track,
                plans_by_aspect=plans_by_aspect,
                frame_types=frame_types,
            )
        except Exception as exc:
            if caption_track is not None:
                assert self._caption_service is not None
                await self._caption_service.mark_failed(session, caption_track.id, str(exc))
            raise
        else:
            if caption_track is not None:
                assert self._caption_service is not None
                await self._caption_service.mark_completed(session, caption_track.id)
        warnings.extend(render_warnings)
        for scene in scenes:
            plans = [
                plan.to_dict()
                for aspect_plans in plans_by_aspect.values()
                for plan in aspect_plans
                if plan.scene_position == scene.position
            ]
            if plans:
                scene.graphic_plan_json = {
                    "aspects": {
                        aspect: next(
                            (
                                plan.to_dict()
                                for plan in aspect_plans
                                if plan.scene_position == scene.position
                            ),
                            {},
                        )
                        for aspect, aspect_plans in plans_by_aspect.items()
                    }
                }
                scene.editing_plan_json = plans[0]

        canonical_aspect = "16:9" if "16:9" in rendered else next(iter(rendered))
        canonical_path, canonical_w, canonical_h, canonical_duration, _ = rendered[canonical_aspect]
        render = existing or VideoRender(
            narration_audio_id=narration.id,
            scene_plan_id=scene_plan.id,
            status="draft",
            renderer_name=self._renderer.name,
            video_path=str(canonical_path),
            subtitle_path=str(subtitle_path),
            timeline_path=str(timeline_path),
            duration_seconds=canonical_duration,
            width=canonical_w,
            height=canonical_h,
            fps=self._settings.video_fps,
        )
        if existing is not None:
            render.scene_plan_id = scene_plan.id
            render.status = "draft"
            render.renderer_name = self._renderer.name
            render.video_path = str(canonical_path)
            render.subtitle_path = str(subtitle_path)
            render.timeline_path = str(timeline_path)
            render.duration_seconds = canonical_duration
            render.width = canonical_w
            render.height = canonical_h
            render.fps = self._settings.video_fps
            await session.execute(
                delete(VideoVariant).where(VideoVariant.video_render_id == render.id)
            )
        else:
            session.add(render)
        await session.flush()
        for aspect, (path, width, height, duration, checks) in rendered.items():
            session.add(
                VideoVariant(
                    video_render_id=render.id,
                    aspect_ratio=aspect,
                    style_key=preset.key,
                    width=width,
                    height=height,
                    fps=self._settings.video_fps,
                    video_path=str(path),
                    subtitle_path=str(subtitle_path),
                    timeline_path=str(timeline_path),
                    duration_seconds=duration,
                    status="draft",
                    checks_json=checks,
                )
            )
        script = await session.get(NewsScript, narration.script_id)
        approval = await get_or_create_approval(session, render, script, self._settings)
        if existing is not None:
            approval.status = "pending"
            approval.decided_at = None
            approval.artifact_sha256 = None
            approval.quality_checks = []
        return warnings

    async def _render_aspects(
        self,
        *,
        requested: list[str],
        output_dir: Path,
        scenes: list[Scene],
        timings: list[Any],
        scene_assets: dict[str, tuple[SceneAsset, VisualAsset | None]],
        preset: EditingPreset,
        audio_path: Path,
        subtitle_path: Path,
        narration: NarrationAudio,
        burn_scene_captions: bool,
        caption_track: Any,
        plans_by_aspect: dict[str, list[Any]] | None = None,
        frame_types: dict[int, str] | None = None,
    ) -> tuple[dict[str, tuple[Path, int, int, float, list[dict[str, str]]]], list[str]]:
        rendered: dict[str, tuple[Path, int, int, float, list[dict[str, str]]]] = {}
        warnings: list[str] = []
        plans_by_aspect = plans_by_aspect or {}
        for aspect in requested:
            width, height = self._size_for(aspect)
            frame_dir = output_dir / "frames" / aspect.replace(":", "x")
            plan_rows = {plan.scene_position: plan for plan in plans_by_aspect.get(aspect, [])}
            clips: list[SceneClip] = []
            overlays: list[Any] = []
            visuals = [
                self._scene_visual(scene, timing, scene_assets.get(scene.id), preset)
                for scene, timing in zip(scenes, timings, strict=True)
            ]
            # Reuse the run-level choice so the frame and its overlays agree.
            for visual in visuals:
                chosen = (frame_types or {}).get(visual.position)
                if chosen:
                    visual.graphic_type = chosen
            for index, (scene, timing, visual) in enumerate(
                zip(scenes, timings, visuals, strict=True)
            ):
                frame_path = frame_dir / f"scene-{scene.position:03d}.jpg"
                frame = render_scene_frame(
                    visual,
                    preset,
                    width,
                    height,
                    frame_path,
                    aspect_ratio=aspect,
                    show_caption=burn_scene_captions,
                    captions_present=burn_scene_captions or caption_track is not None,
                )
                transition = _transition_for(index, len(scenes), preset)
                motion = (
                    "static"
                    if frame.graphic_type
                    in {"map", "chart", "timeline", "diagram", "stat", "quote", "split"}
                    else preset.motion
                )
                clip_video: Path | None = None
                asset_row = scene_assets.get(scene.id)
                if asset_row is not None:
                    scene_asset, asset = asset_row
                    if scene_asset.status == "ready" and asset is not None:
                        candidate = Path(asset.local_path)
                        suffix = candidate.suffix.lower()
                        if candidate.is_file() and (
                            asset.media_type == "video"
                            or suffix in {".mp4", ".mov", ".webm", ".mkv"}
                        ):
                            clip_video = candidate
                            motion = "static"
                clips.append(
                    SceneClip(
                        position=scene.position,
                        image_path=frame.path,
                        duration_seconds=max(0.2, timing.duration_seconds),
                        transition=transition,
                        motion=motion,
                        motion_intensity=preset.motion_intensity,
                        easing=preset.craft.easing,
                        motion_path=preset.craft.motion_path,
                        video_path=clip_video,
                        source_kind="video" if clip_video else "image",
                    )
                )
            graphic_dir = output_dir / "graphics" / aspect.replace(":", "x")
            for plan in plan_rows.values():
                overlays.extend(
                    render_scene_overlays(
                        plan,
                        preset,
                        width=width,
                        height=height,
                        fps=self._settings.video_fps,
                        output_dir=graphic_dir,
                        aspect_ratio=aspect,
                    )
                )
                failed_graphics = [
                    check for check in plan.checks if check.get("status") == "failed"
                ]
                if failed_graphics:
                    raise ValueError(
                        f"{aspect} motion-graphics plan failed: "
                        + "; ".join(
                            f"{check.get('name')}: {check.get('detail')}"
                            for check in failed_graphics
                        )
                    )
            caption_render = await self._caption_render_for(caption_track, aspect)
            video_path = output_dir / f"draft-{aspect.replace(':', 'x')}.mp4"
            render_kwargs: dict[str, Any] = {
                "width": width,
                "height": height,
                "transition_seconds": self._settings.video_transition_seconds,
                "aspect_ratio": aspect,
            }
            if caption_render is not None:
                render_kwargs["caption_render"] = caption_render
            if overlays:
                render_kwargs["overlays"] = overlays
            result = await self._renderer.render(
                clips,
                audio_path,
                subtitle_path,
                video_path,
                narration.duration_seconds,
                **render_kwargs,
            )
            warnings.extend(result.transition_warnings)
            failed = [check for check in result.checks if check.status == "failed"]
            if failed:
                raise ValueError(
                    f"{aspect} render failed validation: "
                    + "; ".join(f"{check.name}: {check.detail}" for check in failed)
                )
            checks = [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in result.checks
            ]
            if caption_render is not None:
                checks.append(
                    {
                        "name": "caption_track",
                        "status": "passed",
                        "detail": (
                            f"{caption_render.cue_count} cue(s); "
                            f"burn-in={caption_render.burns_in}; "
                            f"soft track={caption_render.adds_soft_track}; "
                            f"source={caption_render.source or 'unknown'}."
                        ),
                    }
                )
            # The renderer reports the composited overlay count itself; a second
            # check here would show the same fact twice in the QA panel.
            rendered[aspect] = (
                result.path,
                result.width,
                result.height,
                result.duration_seconds,
                checks,
            )
        return rendered, warnings

    async def _caption_render_for(self, track: Any, aspect: str) -> CaptionRender | None:
        if track is None or self._caption_service is None:
            return None
        ass_path = (
            Path(track.ass_paths[aspect])
            if track.ass_paths and track.ass_paths.get(aspect)
            else None
        )
        soft_path = Path(track.srt_path) if track.srt_path else None
        if (
            ass_path is None
            or not ass_path.is_file()
            or soft_path is None
            or not soft_path.is_file()
        ):
            assets = await self._caption_service.write_assets(track)
            track.srt_path = str(assets.srt_path)
            track.vtt_path = str(assets.vtt_path)
            track.ass_paths = {key: str(value) for key, value in assets.ass_paths.items()}
            ass_path = assets.ass_paths.get(aspect)
            soft_path = assets.srt_path
        return CaptionRender(
            ass_path=ass_path,
            fonts_dir=self._settings.caption_fonts_dir or default_fonts_dir(),
            soft_subtitle_path=soft_path,
            burn_in=self._settings.video_captions_enabled,
            soft_subs=self._settings.caption_soft_subs_default,
            cue_count=len(track.cues_json or []),
            source=track.source,
        )

    async def _load_scene_assets(
        self, session: AsyncSession, scene_ids: list[str]
    ) -> dict[str, tuple[SceneAsset, VisualAsset | None]]:
        rows = list(
            (
                await session.execute(
                    select(SceneAsset, VisualAsset)
                    .outerjoin(VisualAsset, VisualAsset.id == SceneAsset.asset_id)
                    .where(
                        SceneAsset.scene_id.in_(scene_ids),
                        SceneAsset.role == "primary",
                    )
                )
            )
            .tuples()
            .all()
        )
        return {scene_asset.scene_id: (scene_asset, asset) for scene_asset, asset in rows}

    def _frame_types(
        self,
        scenes: list[Scene],
        timings: list[Any],
        scene_assets: dict[str, tuple[SceneAsset, VisualAsset | None]],
        preset: EditingPreset,
    ) -> dict[int, str]:
        """Choose the run's frame templates once, before overlays are planned."""

        visuals = [
            self._scene_visual(scene, timing, scene_assets.get(scene.id), preset)
            for scene, timing in zip(scenes, timings, strict=True)
        ]
        return {
            visual.position: graphic_type
            for visual, graphic_type in zip(
                visuals, choose_graphic_types(visuals, preset), strict=True
            )
        }

    def _scene_visual(
        self,
        scene: Scene,
        timing,
        asset_row: tuple[SceneAsset, VisualAsset | None] | None,
        preset: EditingPreset,
    ) -> SceneVisual:
        asset_path: Path | None = None
        asset_kind = "graphic"
        attribution: str | None = None
        source_url: str | None = None
        focal_x = 0.5
        focal_y = 0.5
        if asset_row is not None:
            scene_asset, asset = asset_row
            if scene_asset.status == "ready" and asset is not None:
                candidate_path = Path(asset.local_path)
                if candidate_path.is_file():
                    suffix = candidate_path.suffix.lower()
                    if asset.media_type == "video" or suffix in {".mp4", ".mov", ".webm", ".mkv"}:
                        asset_kind = "video"
                    else:
                        asset_path = candidate_path
                        asset_kind = "photo"
                    attribution = asset.attribution or asset.creator or asset.license_name
                    source_url = asset.source_url
                    focal_x = asset.focal_x
                    focal_y = asset.focal_y
        return SceneVisual(
            position=scene.position,
            start_seconds=timing.start_seconds,
            duration_seconds=timing.duration_seconds,
            narration=scene.narration,
            subtitle=scene.subtitle,
            headline=headline_for_text(
                scene.subtitle or scene.narration or "",
                max(preset.landscape.max_headline_chars, preset.portrait.max_headline_chars * 2),
            ),
            keywords=list(
                search_keywords(scene.narration or scene.subtitle or "") or scene.keywords or []
            ),
            scene_description=scene.scene_description,
            image_query=scene.image_search_query,
            animation_suggestion=scene.animation_suggestion,
            transition=scene.transition,
            asset_path=asset_path,
            asset_kind=asset_kind,
            attribution=attribution,
            source_url=source_url,
            focal_x=focal_x,
            focal_y=focal_y,
            shot=shot_for_position(scene.position, preset.craft),
        )

    def _size_for(self, aspect: str) -> tuple[int, int]:
        if aspect == "9:16":
            return self._settings.video_portrait_width, self._settings.video_portrait_height
        return self._settings.video_landscape_width, self._settings.video_landscape_height


def _normalize_aspects(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    else:
        items = [str(item).strip() for item in value]
    aspects = [item for item in items if item in ASPECT_SIZES]
    if not aspects:
        aspects = ["16:9"]
    seen: list[str] = []
    for aspect in aspects:
        if aspect not in seen:
            seen.append(aspect)
    return seen


def _transition_for(index: int, total: int, preset: EditingPreset) -> str:
    if index == 0:
        return "fade"
    if index == total - 1:
        return "fade" if preset.transition == "cut" else preset.transition
    if index % 3 == 0:
        return transition_for_scene(
            preset.transition,
            preset.craft,
            scene_index=index,
            is_boundary=True,
        )
    return transition_for_scene("cut", preset.craft, scene_index=index, is_boundary=False)
