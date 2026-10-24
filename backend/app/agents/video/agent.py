from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.video.contracts import VideoRenderer
from backend.app.agents.video.renderer import FFmpegVideoRenderer
from backend.app.agents.video.subtitles import render_srt
from backend.app.core.config import Settings
from backend.app.db.models import (
    AgentRun,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    Scene,
    ScenePlan,
    VideoRender,
)
from backend.app.reviews.service import get_or_create_approval
from backend.app.schemas.video import VideoRunRead


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
        self._renderer = renderer or FFmpegVideoRenderer(
            settings.video_ffmpeg_executable,
            settings.video_width,
            settings.video_height,
            settings.video_fps,
            settings.video_background_color,
        )

    async def execute(self) -> VideoRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                candidates = await self._load_eligible_narrations(session)
                run.items_seen = len(candidates)
                for narration, scene_plan in candidates:
                    await self._render_video(session, narration, scene_plan)
                    run.items_created += 1
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
            raise RuntimeError(run.errors[-1])
        return VideoRunRead.model_validate(run)

    async def _load_eligible_narrations(
        self, session: AsyncSession
    ) -> list[tuple[NarrationAudio, ScenePlan]]:
        statement = (
            select(NarrationAudio, ScenePlan)
            .join(ScenePlan, ScenePlan.id == NarrationAudio.scene_plan_id)
            .join(NewsScript, NewsScript.id == NarrationAudio.script_id)
            .outerjoin(VideoRender, VideoRender.narration_audio_id == NarrationAudio.id)
            .where(
                NarrationAudio.status == "completed",
                ScenePlan.status == "draft",
                NewsScript.status == "draft",
                VideoRender.id.is_(None),
            )
            .order_by(NarrationAudio.created_at.asc())
            .limit(self._settings.video_batch_size)
        )
        return list((await session.execute(statement)).tuples().all())

    async def _render_video(
        self, session: AsyncSession, narration: NarrationAudio, scene_plan: ScenePlan
    ) -> None:
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
                        "start_seconds": scene.start_seconds,
                        "duration_seconds": scene.duration_seconds,
                        "narration": scene.narration,
                        "keywords": scene.keywords,
                        "scene_description": scene.scene_description,
                        "stock_footage_query": scene.stock_footage_query,
                        "image_search_query": scene.image_search_query,
                        "animation_suggestion": scene.animation_suggestion,
                        "transition": scene.transition,
                    }
                    for scene in scenes
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        video_path = output_dir / "draft.mp4"
        await self._renderer.render(
            Path(narration.wav_path), subtitle_path, video_path, narration.duration_seconds
        )
        render = VideoRender(
            narration_audio_id=narration.id,
            scene_plan_id=scene_plan.id,
            status="draft",
            renderer_name=self._renderer.name,
            video_path=str(video_path),
            subtitle_path=str(subtitle_path),
            timeline_path=str(timeline_path),
            duration_seconds=narration.duration_seconds,
            width=self._settings.video_width,
            height=self._settings.video_height,
            fps=self._settings.video_fps,
        )
        session.add(render)
        await session.flush()
        script = await session.get(NewsScript, narration.script_id)
        await get_or_create_approval(session, render, script, self._settings)
