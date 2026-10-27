from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

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
from backend.app.media.craft import shot_for_position, transition_for_scene
from backend.app.media.graphics import SceneVisual, render_scene_frame
from backend.app.media.keywords import search_keywords
from backend.app.media.presets import EditingPreset, get_preset
from backend.app.media.renderer import FFmpegSceneRenderer, SceneClip
from backend.app.media.timeline import align_scenes_to_narration
from backend.app.reviews.service import get_or_create_approval
from backend.app.schemas.video import VideoRunRead

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
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
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
                select(PublishingRecord.id).where(
                    PublishingRecord.video_render_id == existing.id
                )
            )
            if publishing:
                raise ValueError(
                    "This narration already has publishing activity; create a new story "
                    "instead of re-rendering the approved artifact"
                )

        preset = get_preset(style_key or self._settings.video_default_style)
        timings = align_scenes_to_narration(scenes, chunks, narration.duration_seconds)
        scene_assets = await self._load_scene_assets(session, [scene.id for scene in scenes])
        output_dir = self._settings.video_output_dir / narration.id
        output_dir.mkdir(parents=True, exist_ok=True)
        subtitle_path = output_dir / "subtitles.srt"
        subtitle_path.write_text(render_srt(chunks), encoding="utf-8")
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
                    }
                    for scene, timing in zip(scenes, timings, strict=True)
                ],
                indent=2,
            ),
            encoding="utf-8",
        )

        requested = _normalize_aspects(aspects or self._settings.video_aspects)
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
        audio_path = Path(narration.wav_path)
        if music_path is not None and music_path.is_file():
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
                audio_path = mixed_path
        rendered: dict[str, tuple[Path, int, int, float, list[dict[str, str]]]] = {}
        for aspect in requested:
            width, height = self._size_for(aspect)
            frame_dir = output_dir / "frames" / aspect.replace(":", "x")
            clips: list[SceneClip] = []
            for index, (scene, timing) in enumerate(zip(scenes, timings, strict=True)):
                visual = self._scene_visual(
                    scene, timing, scene_assets.get(scene.id), preset
                )
                frame_path = frame_dir / f"scene-{scene.position:03d}.jpg"
                frame = render_scene_frame(
                    visual,
                    preset,
                    width,
                    height,
                    frame_path,
                    aspect_ratio=aspect,
                    show_caption=captions,
                )
                transition = _transition_for(index, len(scenes), preset)
                motion = (
                    "static"
                    if frame.graphic_type
                    in {"map", "chart", "timeline", "diagram", "stat", "quote", "split"}
                    else preset.motion
                )
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
                    )
                )
            video_path = output_dir / f"draft-{aspect.replace(':', 'x')}.mp4"
            result = await self._renderer.render(
                clips,
                audio_path,
                subtitle_path,
                video_path,
                narration.duration_seconds,
                width=width,
                height=height,
                transition_seconds=self._settings.video_transition_seconds,
                aspect_ratio=aspect,
            )
            warnings.extend(result.transition_warnings)
            failed = [check for check in result.checks if check.status == "failed"]
            if failed:
                raise ValueError(
                    f"{aspect} render failed validation: "
                    + "; ".join(f"{check.name}: {check.detail}" for check in failed)
                )
            rendered[aspect] = (
                result.path,
                result.width,
                result.height,
                result.duration_seconds,
                [
                    {"name": check.name, "status": check.status, "detail": check.detail}
                    for check in result.checks
                ],
            )

        canonical_aspect = "16:9" if "16:9" in rendered else next(iter(rendered))
        canonical_path, canonical_w, canonical_h, canonical_duration, _ = rendered[
            canonical_aspect
        ]
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
            ).tuples().all()
        )
        return {scene_asset.scene_id: (scene_asset, asset) for scene_asset, asset in rows}

    def _scene_visual(
        self,
        scene: Scene,
        timing,
        asset_row: tuple[SceneAsset, VisualAsset | None] | None,
        preset: EditingPreset,
    ) -> SceneVisual:
        asset_path: Path | None = None
        attribution: str | None = None
        source_url: str | None = None
        focal_x = 0.5
        focal_y = 0.5
        if asset_row is not None:
            scene_asset, asset = asset_row
            if scene_asset.status == "ready" and asset is not None:
                candidate_path = Path(asset.local_path)
                if candidate_path.is_file():
                    asset_path = candidate_path
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
            headline=scene.subtitle or scene.narration,
            keywords=list(
                search_keywords(scene.narration or scene.subtitle or "")
                or scene.keywords
                or []
            ),
            scene_description=scene.scene_description,
            image_query=scene.image_search_query,
            animation_suggestion=scene.animation_suggestion,
            transition=scene.transition,
            asset_path=asset_path,
            asset_kind="photo" if asset_path else "graphic",
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
    return transition_for_scene(
        "cut", preset.craft, scene_index=index, is_boundary=False
    )
