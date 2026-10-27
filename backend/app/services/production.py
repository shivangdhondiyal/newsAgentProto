from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import Settings
from backend.app.db.models import (
    Article,
    CaptionTrack,
    ContentApproval,
    CredibilityAssessment,
    NarrationAudio,
    NewsScript,
    ProductionJob,
    ProductionStage,
    ResearchBrief,
    Scene,
    SceneAsset,
    ScenePlan,
    VideoRender,
    VideoVariant,
    VisualAsset,
)
from backend.app.media.audio import resolve_executable
from backend.app.media.captions import (
    SOURCE_LABELS,
    CaptionCue,
    CaptionSettings,
    list_caption_presets,
    preset_settings,
)
from backend.app.media.presets import PRESETS, list_presets
from backend.app.media.transcription import transcription_status
from backend.app.reviews.quality import evaluate_video_quality
from backend.app.schemas.production import ProductionJobCreate
from backend.app.services.run_control import AgentRunCoordinator

logger = logging.getLogger(__name__)

STAGE_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("prepare", "Prepare script"),
    ("scenes", "Plan scenes"),
    ("assets", "Source visuals"),
    ("narrate", "Narrate"),
    ("captions", "Generate captions"),
    ("render", "Render 16:9 + 9:16"),
    ("verify", "Verify outputs"),
)

DESTINATIONS = ["local", "youtube"]
ASSET_STRATEGIES = ["hybrid", "licensed_only", "graphics_only"]


class ProductionError(ValueError):
    """A production request cannot be fulfilled as specified."""


class ProductionNotFound(ProductionError):
    pass


class ProductionConflict(ProductionError):
    pass


@dataclass(frozen=True)
class StageResult:
    message: str
    level: str = "info"
    warnings: tuple[str, ...] = ()


class ProductionService:
    """Resumable, one-click production orchestration with persisted stage state."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        coordinator: AgentRunCoordinator,
        *,
        script_writer_agent: Any,
        scene_planner_agent: Any,
        asset_agent: Any,
        tts_agent: Any,
        video_agent: Any,
        caption_service: Any | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._coordinator = coordinator
        self._script_writer = script_writer_agent
        self._scene_planner = scene_planner_agent
        self._asset_agent = asset_agent
        self._tts_agent = tts_agent
        self._video_agent = video_agent
        self._caption_service = caption_service
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._create_lock = asyncio.Lock()

    # ------------------------------------------------------------------ creation

    async def create_job(self, payload: ProductionJobCreate) -> ProductionJob:
        if payload.style_key not in PRESETS:
            raise ProductionError(f"Unknown editing style: {payload.style_key}")
        if payload.asset_strategy not in ASSET_STRATEGIES:
            raise ProductionError(f"Unknown asset strategy: {payload.asset_strategy}")
        if payload.destination not in DESTINATIONS:
            raise ProductionError(f"Unknown destination: {payload.destination}")
        if payload.music_path and not Path(payload.music_path).is_file():
            raise ProductionError(f"Music file not found: {payload.music_path}")

        async with self._create_lock:
            async with self._session_factory() as session:
                script = await self._resolve_script(
                    session, payload.script_id, payload.article_id
                )
                if script is None:
                    raise ProductionError(
                        "No draft script is available for this story. Run the script "
                        "stage first, then start production."
                    )
                fingerprint = _fingerprint(
                    script.id,
                    payload.style_key,
                    payload.aspect_ratio,
                    payload.captions,
                    payload.asset_strategy,
                    payload.voice,
                    payload.destination,
                    payload.music_path,
                    payload.regenerate_narration,
                )
                active = await session.scalar(
                    select(ProductionJob)
                    .where(
                        ProductionJob.fingerprint == fingerprint,
                        ProductionJob.status.in_(("queued", "running")),
                    )
                    .order_by(ProductionJob.created_at.desc())
                )
                if active is not None:
                    return active
                recent = await session.scalar(
                    select(ProductionJob)
                    .where(
                        ProductionJob.fingerprint == fingerprint,
                        ProductionJob.status.in_(("awaiting_approval", "completed")),
                        ProductionJob.updated_at
                        >= datetime.now(UTC) - timedelta(minutes=10),
                    )
                    .order_by(ProductionJob.created_at.desc())
                )
                if recent is not None:
                    return recent
                job = ProductionJob(
                    status="queued",
                    article_id=script.article_id,
                    script_id=script.id,
                    style_key=payload.style_key,
                    aspect_ratio=payload.aspect_ratio,
                    fingerprint=fingerprint,
                    requested_by=payload.requested_by,
                    config_json={
                        "captions": payload.captions,
                        "asset_strategy": payload.asset_strategy,
                        "voice": payload.voice,
                        "destination": payload.destination,
                        "music_path": payload.music_path,
                        "aspect_ratio": payload.aspect_ratio,
                        "style_key": payload.style_key,
                        "regenerate_narration": payload.regenerate_narration,
                    },
                    logs=[],
                    warnings=[],
                )
                session.add(job)
                await session.flush()
                for key, label in STAGE_DEFINITIONS:
                    session.add(
                        ProductionStage(
                            job_id=job.id,
                            key=key,
                            label=label,
                            status="queued",
                            logs=[],
                        )
                    )
                await session.commit()
                await session.refresh(job)
                return job

    async def _resolve_script(
        self, session: AsyncSession, script_id: str | None, article_id: str | None
    ) -> NewsScript | None:
        if script_id:
            script = await session.get(NewsScript, script_id)
            if script is None:
                raise ProductionNotFound("Script not found")
            return script
        statement = select(NewsScript).order_by(NewsScript.created_at.desc())
        if article_id:
            statement = statement.where(NewsScript.article_id == article_id)
        else:
            statement = statement.where(NewsScript.status == "draft")
        return await session.scalar(statement.limit(1))

    async def start_job(self, job_id: str) -> ProductionJob:
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            if job is None:
                raise ProductionNotFound("Production job not found")
            if job.status == "running":
                raise ProductionConflict("This production job is already running")
            if job.status in {"awaiting_approval", "completed"}:
                raise ProductionConflict("This production job is already complete")
            stages = list(
                (
                    await session.scalars(
                        select(ProductionStage)
                        .where(ProductionStage.job_id == job_id)
                        .order_by(ProductionStage.created_at)
                    )
                ).all()
            )
            _requeue_unfinished(stages)
            job.status = "running"
            job.started_at = job.started_at or datetime.now(UTC)
            job.finished_at = None
            job.error = None
            job.logs = [
                *(job.logs or []),
                _log_entry("info", "Production run started."),
            ]
            await session.commit()
        self._spawn(job_id)
        return job

    def _spawn(self, job_id: str) -> None:
        existing = self._tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._run_job(job_id), name=f"production:{job_id}")
        self._tasks[job_id] = task

        def _finished(_task: asyncio.Task[None]) -> None:
            self._tasks.pop(job_id, None)

        task.add_done_callback(_finished)

    async def rerender_job(self, job_id: str) -> ProductionJob:
        """Re-run only render + verify after caption edits or settings changes."""

        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            if job is None:
                raise ProductionNotFound("Production job not found")
            if job.status == "running":
                raise ProductionConflict("This production job is already running")
            stages = list(
                (
                    await session.scalars(
                        select(ProductionStage)
                        .where(ProductionStage.job_id == job_id)
                        .order_by(ProductionStage.created_at)
                    )
                ).all()
            )
            targets = [stage for stage in stages if stage.key in {"render", "verify"}]
            if not targets:
                raise ProductionConflict(
                    "This job has no render stage to re-run. Start a new production."
                )
            for stage in targets:
                stage.status = "queued"
                stage.error = None
                stage.finished_at = None
                stage.duration_ms = None
                stage.logs = [
                    *(stage.logs or []),
                    _log_entry("info", "Re-queued to re-render with the current captions."),
                ]
            job.status = "running"
            job.current_stage = None
            job.error = None
            job.finished_at = None
            job.logs = [
                *(job.logs or []),
                _log_entry(
                    "info",
                    "Re-rendering both aspect ratios with the current caption track.",
                ),
            ]
            await session.commit()
        self._spawn(job_id)
        return job

    async def retry_job(self, job_id: str) -> ProductionJob:
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            if job is None:
                raise ProductionNotFound("Production job not found")
            if job.status == "running":
                raise ProductionConflict("This production job is already running")
            stages = list(
                (
                    await session.scalars(
                        select(ProductionStage)
                        .where(ProductionStage.job_id == job_id)
                        .order_by(ProductionStage.created_at)
                    )
                ).all()
            )
            failed = next(
                (
                    stage
                    for stage in stages
                    if stage.status in {"failed", "interrupted"}
                ),
                None,
            )
            if failed is None:
                raise ProductionConflict("There is no failed stage to retry")
            _requeue_unfinished(stages)
            job.status = "running"
            job.error = None
            job.finished_at = None
            job.logs = [*(job.logs or []), _log_entry("info", f"Retrying {failed.label}.")]
            await session.commit()
        self._spawn(job_id)
        return job

    # ----------------------------------------------------------------- execution

    async def _run_job(self, job_id: str) -> None:
        try:
            while True:
                async with self._session_factory() as session:
                    job = await session.get(ProductionJob, job_id)
                    if job is None:
                        return
                    stage = await session.scalar(
                        select(ProductionStage)
                        .where(
                            ProductionStage.job_id == job_id,
                            ProductionStage.status == "queued",
                        )
                        .order_by(ProductionStage.created_at)
                        .limit(1)
                    )
                    if stage is None:
                        job.status = "awaiting_approval"
                        job.current_stage = None
                        job.finished_at = datetime.now(UTC)
                        job.logs = [
                            *(job.logs or []),
                            _log_entry(
                                "success",
                                "Production finished. Human approval is required "
                                "before publishing.",
                            ),
                        ]
                        await session.commit()
                        return
                    stage.status = "running"
                    stage.started_at = datetime.now(UTC)
                    stage.attempts += 1
                    stage.error = None
                    job.current_stage = stage.key
                    job.logs = [
                        *(job.logs or []),
                        _log_entry("info", f"Stage started: {stage.label}."),
                    ]
                    stage.logs = [
                        *(stage.logs or []),
                        _log_entry("info", f"Attempt {stage.attempts} started."),
                    ]
                    stage_key = stage.key
                    stage_label = stage.label
                    await session.commit()

                started = datetime.now(UTC)
                try:
                    result = await self._execute_stage(job_id, stage_key)
                except Exception as exc:  # noqa: BLE001 - persisted as stage failure
                    logger.exception("Production stage failed", extra={"stage": stage_key})
                    async with self._session_factory() as session:
                        job = await session.get(ProductionJob, job_id)
                        stage = await session.scalar(
                            select(ProductionStage).where(
                                ProductionStage.job_id == job_id,
                                ProductionStage.key == stage_key,
                            )
                        )
                        if stage is not None:
                            stage.status = "failed"
                            stage.error = str(exc)
                            stage.finished_at = datetime.now(UTC)
                            stage.duration_ms = _elapsed_ms(started)
                            stage.logs = [
                                *(stage.logs or []),
                                _log_entry("error", str(exc)),
                            ]
                        if job is not None:
                            job.status = "failed"
                            job.error = f"{stage_label}: {exc}"
                            job.finished_at = datetime.now(UTC)
                            job.logs = [
                                *(job.logs or []),
                                _log_entry("error", f"{stage_label} failed: {exc}"),
                            ]
                        await session.commit()
                    return

                async with self._session_factory() as session:
                    job = await session.get(ProductionJob, job_id)
                    stage = await session.scalar(
                        select(ProductionStage).where(
                            ProductionStage.job_id == job_id,
                            ProductionStage.key == stage_key,
                        )
                    )
                    if stage is not None:
                        stage.status = "completed"
                        stage.finished_at = datetime.now(UTC)
                        stage.duration_ms = _elapsed_ms(started)
                        stage.logs = [
                            *(stage.logs or []),
                            _log_entry(result.level, result.message),
                        ]
                    if job is not None:
                        job.logs = [
                            *(job.logs or []),
                            _log_entry(result.level, result.message),
                        ]
                        if result.warnings:
                            known = list(job.warnings or [])
                            for warning in result.warnings:
                                if warning and warning not in known:
                                    known.append(warning)
                            job.warnings = known
                    await session.commit()
        except asyncio.CancelledError:
            async with self._session_factory() as session:
                job = await session.get(ProductionJob, job_id)
                if job is not None and job.status == "running":
                    job.status = "interrupted"
                    job.error = "Production was interrupted during application shutdown."
                    job.logs = [
                        *(job.logs or []),
                        _log_entry("warning", "Production interrupted."),
                    ]
                    await session.commit()
            raise
        except Exception as exc:  # noqa: BLE001 - last-resort job failure
            logger.exception("Production job crashed", extra={"job_id": job_id})
            async with self._session_factory() as session:
                job = await session.get(ProductionJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = str(exc)
                    job.finished_at = datetime.now(UTC)
                    job.logs = [*(job.logs or []), _log_entry("error", str(exc))]
                    await session.commit()

    async def _execute_stage(self, job_id: str, stage_key: str) -> StageResult:
        if stage_key == "prepare":
            return await self._stage_prepare(job_id)
        if stage_key == "scenes":
            return await self._stage_scenes(job_id)
        if stage_key == "assets":
            return await self._stage_assets(job_id)
        if stage_key == "narrate":
            return await self._stage_narrate(job_id)
        if stage_key == "captions":
            return await self._stage_captions(job_id)
        if stage_key == "render":
            return await self._stage_render(job_id)
        if stage_key == "verify":
            return await self._stage_verify(job_id)
        raise ProductionError(f"Unknown production stage: {stage_key}")

    async def _stage_prepare(self, job_id: str) -> StageResult:
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            if job is None:
                raise ProductionNotFound("Production job not found")
            script = await session.get(NewsScript, job.script_id) if job.script_id else None
            if script is not None:
                if script.status != "draft":
                    raise ProductionError(
                        f"Script {script.id} is '{script.status}', not 'draft'. Resolve the "
                        "review reasons before producing a video."
                    )
                return StageResult(f"Using existing draft script: {script.title}")
            if not job.article_id:
                raise ProductionError("The job has neither a script nor an article")
        await self._coordinator.run(
            "script_writer",
            self._script_writer.execute,
            wait_if_busy=True,
            label="Write scripts",
        )
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            script = await session.scalar(
                select(NewsScript)
                .where(NewsScript.article_id == job.article_id)
                .order_by(NewsScript.created_at.desc())
            )
            if script is None:
                raise ProductionError(
                    "No script was generated for this article. Check the research brief "
                    "and script-writer logs."
                )
            if script.status != "draft":
                raise ProductionError(
                    f"Generated script is '{script.status}' and needs editorial review "
                    "before production."
                )
            job.script_id = script.id
            await session.commit()
            return StageResult(f"Generated draft script: {script.title}")

    async def _stage_scenes(self, job_id: str) -> StageResult:
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            plan = await session.scalar(
                select(ScenePlan).where(ScenePlan.script_id == job.script_id)
            )
            if plan is not None:
                count = await session.scalar(
                    select(Scene.id).where(Scene.scene_plan_id == plan.id).limit(1)
                )
                if count is not None:
                    return StageResult("Using existing scene plan with timed scenes.")
        await self._coordinator.run(
            "scene_planner",
            self._scene_planner.execute,
            wait_if_busy=True,
            label="Plan scenes",
        )
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            plan = await session.scalar(
                select(ScenePlan).where(ScenePlan.script_id == job.script_id)
            )
            if plan is None:
                raise ProductionError("Scene planning did not produce a plan")
            scenes = list(
                (
                    await session.scalars(
                        select(Scene).where(Scene.scene_plan_id == plan.id)
                    )
                ).all()
            )
            if not scenes:
                raise ProductionError("Scene planning produced no scenes")
            return StageResult(f"Planned {len(scenes)} scenes.")

    async def _stage_assets(self, job_id: str) -> StageResult:
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            plan = await session.scalar(
                select(ScenePlan).where(ScenePlan.script_id == job.script_id)
            )
            if plan is None:
                raise ProductionError("Scene plan is missing")
            scene_ids = list(
                (
                    await session.scalars(
                        select(Scene.id).where(Scene.scene_plan_id == plan.id)
                    )
                ).all()
            )
            existing = list(
                (
                    await session.scalars(
                        select(SceneAsset).where(SceneAsset.scene_id.in_(scene_ids))
                    )
                ).all()
            )
            missing = await _missing_asset_files(session, scene_ids) if scene_ids else []
            if len(existing) >= len(scene_ids) and scene_ids and not missing:
                return _asset_stage_result(existing)
            aspects = _aspects_for(job.aspect_ratio)
            strategy = (job.config_json or {}).get(
                "asset_strategy", self._settings.asset_strategy_default
            )
            script_id = job.script_id
        await self._coordinator.run(
            "asset_retriever",
            lambda: self._asset_agent.execute(
                script_id=script_id,
                strategy=strategy,
                aspect_ratio="9:16" if aspects == ["9:16"] else "16:9",
            ),
            wait_if_busy=True,
            label="Source visuals",
        )
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            plan = await session.scalar(
                select(ScenePlan).where(ScenePlan.script_id == job.script_id)
            )
            scene_ids = list(
                (
                    await session.scalars(
                        select(Scene.id).where(Scene.scene_plan_id == plan.id)
                    )
                ).all()
            )
            rows = list(
                (
                    await session.scalars(
                        select(SceneAsset).where(SceneAsset.scene_id.in_(scene_ids))
                    )
                ).all()
            )
            result = _asset_stage_result(rows)
            if missing:
                result = StageResult(
                    result.message,
                    level=result.level,
                    warnings=(
                        *result.warnings,
                        (
                            f"{len(missing)} cached asset file(s) were missing and "
                            "re-retrieved."
                        ),
                    ),
                )
            return result

    async def _stage_narrate(self, job_id: str) -> StageResult:
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            narration = await session.scalar(
                select(NarrationAudio).where(NarrationAudio.script_id == job.script_id)
            )
            force = bool((job.config_json or {}).get("regenerate_narration"))
            if narration is not None and not force:
                if narration.wav_path and Path(narration.wav_path).is_file():
                    return StageResult(
                        f"Using mastered narration ({narration.duration_seconds:.1f}s, "
                        f"{narration.sample_rate} Hz)."
                    )
                # The database row survived but the audio file did not (cleanup,
                # restore, or a sync conflict). Re-narrate instead of handing a
                # missing path to the renderer.
                force = True
                job.warnings = [
                    *(job.warnings or []),
                    (
                        "Mastered narration was missing from disk; it was "
                        "regenerated before rendering."
                    ),
                ]
                await session.commit()
            if narration is not None and force:
                render = await session.scalar(
                    select(VideoRender).where(
                        VideoRender.narration_audio_id == narration.id
                    )
                )
                if render is not None:
                    job.warnings = [
                        *(job.warnings or []),
                        "Re-narration invalidated the previous draft render; both "
                        "aspect ratios are rebuilt from the new audio.",
                    ]
                    await session.commit()
            script_id = job.script_id
        await self._coordinator.run(
            "tts",
            lambda: self._tts_agent.execute(script_ids=[script_id], force=force),
            wait_if_busy=True,
            label="Narrate",
        )
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            narration = await session.scalar(
                select(NarrationAudio).where(NarrationAudio.script_id == job.script_id)
            )
            if narration is None:
                raise ProductionError("Narration was not produced for this script")
            if force and self._caption_service is not None:
                await self._caption_service.mark_stale_for_narration(
                    session,
                    narration.id,
                    "Narration was regenerated; captions may no longer match the new audio.",
                )
            return StageResult(
                f"Narrated {narration.duration_seconds:.1f}s at {narration.sample_rate} Hz."
            )

    async def _stage_captions(self, job_id: str) -> StageResult:
        if self._caption_service is None:
            return StageResult(
                "Caption generation is not configured; the renderer will use scene captions.",
                level="warning",
            )
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            if job is None:
                raise ProductionNotFound("Production job not found")
            if not bool((job.config_json or {}).get("captions", True)):
                return StageResult("Captions are disabled for this job.")
            narration = await session.scalar(
                select(NarrationAudio).where(NarrationAudio.script_id == job.script_id)
            )
            if narration is None:
                raise ProductionError(
                    "Narration is missing, so captions cannot be aligned to speech. "
                    "Run the narration stage first."
                )
            track = await self._caption_service.get_for_narration(session, narration.id)
            if track is not None and track.cues_json and not track.stale:
                return StageResult(
                    f"Using existing caption track ({len(track.cues_json)} cue(s), "
                    f"{track.source})."
                )
            if track is not None and track.stale and track.edited:
                raise ProductionError(
                    "The narration changed after these captions were manually edited. "
                    "Open the caption editor, review the stale track, and choose "
                    "Regenerate to confirm replacing the edits before re-rendering."
                )
            try:
                track = await self._caption_service.ensure_for_narration(
                    session, narration
                )
            except ValueError as exc:
                raise ProductionError(
                    f"Caption generation failed: {exc}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - surfaced as an actionable stage error
                raise ProductionError(
                    "Caption generation failed unexpectedly: "
                    f"{exc}. Check the narration audio and caption settings, then retry."
                ) from exc
            return StageResult(
                f"Generated {len(track.cues_json or [])} caption cue(s) from "
                f"{track.source_detail or track.source}."
            )

    async def _stage_render(self, job_id: str) -> StageResult:
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            if job is None:
                raise ProductionNotFound("Production job not found")
            narration = await session.scalar(
                select(NarrationAudio).where(NarrationAudio.script_id == job.script_id)
            )
            if narration is None:
                raise ProductionError("Narration is missing")
            aspects = _aspects_for(job.aspect_ratio)
            music_path = (job.config_json or {}).get("music_path")
            captions = bool((job.config_json or {}).get("captions", True))
            narration_id = narration.id
        await self._coordinator.run(
            "video_generator",
            lambda: self._video_agent.execute(
                style_key=job.style_key,
                aspects=aspects,
                narration_id=narration_id,
                music_path=Path(music_path) if music_path else None,
                captions=captions,
            ),
            wait_if_busy=True,
            label="Render video",
        )
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            render = await session.scalar(
                select(VideoRender).where(VideoRender.narration_audio_id == narration_id)
            )
            if render is None:
                raise ProductionError("Video generation did not produce a render")
            job.video_render_id = render.id
            await session.commit()
            variants = list(
                (
                    await session.scalars(
                        select(VideoVariant).where(VideoVariant.video_render_id == render.id)
                    )
                ).all()
            )
            return StageResult(
                "Rendered "
                + ", ".join(
                    f"{variant.aspect_ratio} {variant.width}x{variant.height}"
                    for variant in variants
                )
            )

    async def _stage_verify(self, job_id: str) -> StageResult:
        warnings: list[str] = []
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            if job is None or job.video_render_id is None:
                raise ProductionError("No video render is attached to this job")
            render = await session.get(VideoRender, job.video_render_id)
            if render is None:
                raise ProductionError("Video render record is missing")
            script = await session.get(NewsScript, job.script_id)
            variants = list(
                (
                    await session.scalars(
                        select(VideoVariant).where(
                            VideoVariant.video_render_id == render.id
                        )
                    )
                ).all()
            )
            expected = _aspects_for(job.aspect_ratio)
            missing = [
                aspect for aspect in expected if aspect not in {v.aspect_ratio for v in variants}
            ]
            if missing:
                raise ProductionError(
                    f"Missing required output variant(s): {', '.join(missing)}"
                )
            for variant in variants:
                failed = [
                    check
                    for check in (variant.checks_json or [])
                    if check.get("status") == "failed"
                ]
                if failed:
                    raise ProductionError(
                        f"{variant.aspect_ratio} failed validation: "
                        + "; ".join(f"{item.get('name')}: {item.get('detail')}" for item in failed)
                    )
                if not Path(variant.video_path).is_file():
                    raise ProductionError(
                        f"{variant.aspect_ratio} output file is missing on disk"
                    )
            captions_enabled = bool((job.config_json or {}).get("captions", True))
            if captions_enabled and self._caption_service is not None:
                track = await self._caption_service.get_for_script(session, job.script_id)
                if track is None or not track.cues_json:
                    raise ProductionError(
                        "Captions are enabled but no caption track exists for this job. "
                        "Generate captions in the editor, then re-render."
                    )
                if track.stale:
                    raise ProductionError(
                        "The caption track is stale because the narration changed. "
                        "Regenerate captions in the editor, then re-render."
                    )
                if track.status not in {"ready", "edited", "completed"}:
                    raise ProductionError(
                        f"The caption track is '{track.status}', not ready for review. "
                        "Open the caption editor and resolve the reported checks."
                    )
                if not track.srt_path or not Path(track.srt_path).is_file():
                    raise ProductionError(
                        "The caption SRT sidecar is missing on disk. Re-save the caption "
                        "track, then re-render."
                    )
                narration = await session.get(
                    NarrationAudio, track.narration_audio_id
                ) if track.narration_audio_id else None
                if narration is not None:
                    cues = [
                        CaptionCue.from_dict(item) for item in (track.cues_json or [])
                    ]
                    last_end = max((cue.end for cue in cues), default=0.0)
                    drift = abs(last_end - float(narration.duration_seconds or 0.0))
                    if drift > 2.0:
                        raise ProductionError(
                            f"The final caption ends {drift:.2f}s from the end of the "
                            "narration. Re-time or regenerate the captions before "
                            "re-rendering."
                        )
                for variant in variants:
                    if not any(
                        check.get("name") == "caption_track"
                        for check in (variant.checks_json or [])
                    ):
                        warnings.append(
                            f"{variant.aspect_ratio}: no caption check was recorded; "
                            "confirm captions in the rendered file."
                        )
            quality = evaluate_video_quality(
                render, script, self._settings.approval_min_video_bytes
            )
            failed_quality = [check for check in quality if check.get("status") == "failed"]
            if failed_quality:
                raise ProductionError(
                    "Quality gate failed: "
                    + "; ".join(
                        f"{check.get('name')}: {check.get('detail')}" for check in failed_quality
                    )
                )
            approval = await session.scalar(
                select(ContentApproval).where(ContentApproval.video_render_id == render.id)
            )
            plan = await session.scalar(
                select(ScenePlan).where(ScenePlan.script_id == job.script_id)
            )
            if plan is not None:
                scene_ids = list(
                    (
                        await session.scalars(
                            select(Scene.id).where(Scene.scene_plan_id == plan.id)
                        )
                    ).all()
                )
                fallbacks = list(
                    (
                        await session.scalars(
                            select(SceneAsset).where(
                                SceneAsset.scene_id.in_(scene_ids),
                                SceneAsset.status == "fallback",
                            )
                        )
                    ).all()
                )
                if fallbacks:
                    warnings.append(
                        f"{len(fallbacks)} scene(s) use designed-graphic fallbacks instead "
                        "of retrieved photography. Review the licensing panel."
                    )
            if (job.config_json or {}).get("music_path"):
                warnings.append(
                    "Music was supplied by the operator; confirm its license covers this "
                    "distribution channel."
                )
            job.warnings = [*(job.warnings or []), *warnings]
            await session.commit()
            approval_text = approval.status if approval else "pending"
            return StageResult(
                f"Verified {len(variants)} variant(s). Approval is {approval_text}.",
                warnings=tuple(warnings),
            )

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


def _aspects_for(value: str) -> list[str]:
    if value == "both":
        return ["16:9", "9:16"]
    return [value]


async def _caption_payload(
    session: AsyncSession, job: ProductionJob, script: NewsScript | None
) -> dict[str, Any] | None:
    """Dashboard-safe caption summary; the full editor uses the caption API."""

    if script is None:
        return None
    narration = await session.scalar(
        select(NarrationAudio).where(NarrationAudio.script_id == script.id)
    )
    track = await session.scalar(
        select(CaptionTrack).where(CaptionTrack.script_id == script.id)
    )
    if track is None:
        return {
            "id": None,
            "script_id": script.id,
            "status": "not_generated",
            "preset_key": None,
            "language": None,
            "source": None,
            "source_label": None,
            "source_detail": None,
            "edited": False,
            "stale": False,
            "stale_reason": None,
            "revision": 0,
            "cues": [],
            "settings": None,
            "checks": [],
            "warnings": [],
            "duration_seconds": narration.duration_seconds if narration else None,
            "srt_url": None,
            "vtt_url": None,
            "ass_urls": {},
            "can_generate": narration is not None,
            "blocking_reason": (
                None if narration is not None else "Generate the narration first."
            ),
        }
    settings = CaptionSettings.from_dict(
        track.settings_json, preset_settings(track.preset_key)
    )
    cues = [CaptionCue.from_dict(item) for item in (track.cues_json or [])]
    return {
        "id": track.id,
        "script_id": track.script_id,
        "status": track.status,
        "preset_key": track.preset_key,
        "language": track.language,
        "source": track.source,
        "source_label": SOURCE_LABELS.get(track.source, track.source),
        "source_detail": track.source_detail,
        "edited": bool(track.edited),
        "stale": bool(track.stale),
        "stale_reason": track.stale_reason,
        "revision": track.revision,
        "cues": [cue.to_dict() for cue in cues],
        "settings": settings.to_dict(),
        "checks": track.checks_json or [],
        "warnings": track.warnings or [],
        "duration_seconds": narration.duration_seconds if narration else None,
        "srt_url": f"/api/v1/captions/tracks/{track.id}/export/srt",
        "vtt_url": f"/api/v1/captions/tracks/{track.id}/export/vtt",
        "ass_urls": {
            aspect: f"/api/v1/captions/tracks/{track.id}/export/ass?aspect={aspect}"
            for aspect in ("16:9", "9:16")
        },
        "can_generate": narration is not None,
        "blocking_reason": None,
    }


async def _missing_asset_files(
    session: AsyncSession, scene_ids: list[str]
) -> list[SceneAsset]:
    """Ready asset rows whose cached file has disappeared from disk."""

    rows = (
        await session.execute(
            select(SceneAsset, VisualAsset)
            .outerjoin(VisualAsset, VisualAsset.id == SceneAsset.asset_id)
            .where(
                SceneAsset.scene_id.in_(scene_ids),
                SceneAsset.status == "ready",
            )
        )
    ).all()
    return [
        scene_asset
        for scene_asset, asset in rows
        if asset is None or not Path(asset.local_path).is_file()
    ]


def _asset_stage_result(rows: list[SceneAsset]) -> StageResult:
    ready = sum(1 for row in rows if row.status == "ready")
    fallback = sum(1 for row in rows if row.status == "fallback")
    warnings: list[str] = []
    if fallback:
        warnings.append(
            f"{fallback} scene(s) will use a designed graphic because no licensed asset "
            "passed validation."
        )
    return StageResult(
        f"{ready} licensed asset(s), {fallback} designed fallback(s).",
        warnings=tuple(warnings),
    )


def _fingerprint(*values: Any) -> str:
    payload = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _elapsed_ms(started: datetime) -> float:
    return round((datetime.now(UTC) - started).total_seconds() * 1_000, 2)


def _log_entry(level: str, message: str) -> dict[str, str]:
    return {
        "at": datetime.now(UTC).isoformat(),
        "level": level,
        "message": message,
    }


def _requeue_unfinished(stages: list[ProductionStage]) -> None:
    """Put failed or interrupted stages back in the queue for a resume.

    ``_run_job`` only picks up ``queued`` stages, so without this an
    interrupted run would skip the stage that was mid-flight and fail later
    with a confusing downstream error.
    """

    for stage in stages:
        if stage.status not in {"failed", "interrupted"}:
            continue
        stage.status = "queued"
        stage.error = None
        stage.finished_at = None
        stage.duration_ms = None
        stage.logs = [
            *(stage.logs or []),
            _log_entry("info", "Stage requeued after an interrupted or failed attempt."),
        ]


async def reconcile_stale_production_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Close jobs that were running when the process stopped."""

    async with session_factory() as session:
        jobs = list(
            (
                await session.scalars(
                    select(ProductionJob).where(ProductionJob.status == "running")
                )
            ).all()
        )
        if not jobs:
            return 0
        stages = list(
            (
                await session.scalars(
                    select(ProductionStage).where(
                        ProductionStage.job_id.in_([job.id for job in jobs]),
                        ProductionStage.status == "running",
                    )
                )
            ).all()
        )
        for stage in stages:
            stage.status = "interrupted"
            stage.error = "The process stopped while this stage was running."
            stage.finished_at = datetime.now(UTC)
        for job in jobs:
            job.status = "interrupted"
            job.error = "The process stopped while this production job was running."
            job.finished_at = datetime.now(UTC)
            job.logs = [
                *(job.logs or []),
                _log_entry("warning", "Startup reconciliation marked this job interrupted."),
            ]
        await session.commit()
        return len(jobs)


async def build_job_detail(
    session: AsyncSession,
    job: ProductionJob,
    settings: Settings,
) -> dict[str, Any]:
    """Assemble the dashboard payload, including scenes, variants, and licensing."""

    stages = list(
        (
            await session.scalars(
                select(ProductionStage)
                .where(ProductionStage.job_id == job.id)
                .order_by(ProductionStage.created_at)
            )
        ).all()
    )
    variants = []
    render = None
    approval = None
    if job.video_render_id:
        render = await session.get(VideoRender, job.video_render_id)
        if render is not None:
            variants = list(
                (
                    await session.scalars(
                        select(VideoVariant)
                        .where(VideoVariant.video_render_id == render.id)
                        .order_by(VideoVariant.aspect_ratio)
                    )
                ).all()
            )
            approval = await session.scalar(
                select(ContentApproval).where(ContentApproval.video_render_id == render.id)
            )
    scenes: list[dict[str, Any]] = []
    script = await session.get(NewsScript, job.script_id) if job.script_id else None
    article = await session.get(Article, job.article_id) if job.article_id else None
    if script is not None:
        plan = await session.scalar(
            select(ScenePlan).where(ScenePlan.script_id == script.id)
        )
        if plan is not None:
            scene_rows = list(
                (
                    await session.scalars(
                        select(Scene)
                        .where(Scene.scene_plan_id == plan.id)
                        .order_by(Scene.position)
                    )
                ).all()
            )
            asset_rows = {
                row.scene_id: row
                for row in (
                    await session.scalars(
                        select(SceneAsset).where(
                            SceneAsset.scene_id.in_([scene.id for scene in scene_rows])
                        )
                    )
                ).all()
            }
            assets = {
                asset.id: asset
                for asset in (
                    await session.scalars(
                        select(VisualAsset).where(
                            VisualAsset.id.in_(
                                [
                                    row.asset_id
                                    for row in asset_rows.values()
                                    if row.asset_id
                                ]
                            )
                        )
                    )
                ).all()
            }
            frame_root = settings.video_output_dir / (
                render.narration_audio_id if render else ""
            ) / "frames"
            timings: dict[int, tuple[float, float]] = {}
            if variants:
                timeline_path = Path(variants[0].timeline_path)
                if timeline_path.is_file():
                    try:
                        for entry in json.loads(timeline_path.read_text("utf-8")):
                            timings[int(entry["position"])] = (
                                float(entry["start_seconds"]),
                                float(entry["duration_seconds"]),
                            )
                    except (OSError, ValueError, KeyError, TypeError):
                        timings = {}
            for scene in scene_rows:
                row = asset_rows.get(scene.id)
                asset = assets.get(row.asset_id) if row and row.asset_id else None
                frame_16 = frame_root / "16x9" / f"scene-{scene.position:03d}.jpg"
                frame_9 = frame_root / "9x16" / f"scene-{scene.position:03d}.jpg"
                # Prefer the rendered narration-aligned timing over the planner's
                # estimate, so the dashboard matches the video that was produced.
                start_seconds, duration_seconds = timings.get(
                    scene.position, (scene.start_seconds, scene.duration_seconds)
                )
                scenes.append(
                    {
                        "position": scene.position,
                        "start_seconds": start_seconds,
                        "duration_seconds": duration_seconds,
                        "timed_to_narration": scene.position in timings,
                        "subtitle": scene.subtitle,
                        "narration": scene.narration,
                        "transition": scene.transition,
                        "frame_16x9": str(frame_16) if frame_16.is_file() else None,
                        "frame_9x16": str(frame_9) if frame_9.is_file() else None,
                        "asset": (
                            {
                                "scene_position": scene.position,
                                "scene_id": scene.id,
                                "status": row.status,
                                "match_reason": row.match_reason,
                                "title": asset.title if asset else None,
                                "provider": asset.provider if asset else None,
                                "creator": asset.creator if asset else None,
                                "license_name": asset.license_name if asset else None,
                                "license_url": asset.license_url if asset else None,
                                "source_url": asset.source_url if asset else None,
                                "local_path": asset.local_path if asset else None,
                            }
                            if row
                            else None
                        ),
                    }
                )
    variant_payload = [
        {
            "id": variant.id,
            "aspect_ratio": variant.aspect_ratio,
            "style_key": variant.style_key,
            "width": variant.width,
            "height": variant.height,
            "fps": variant.fps,
            "video_path": variant.video_path,
            "subtitle_path": variant.subtitle_path,
            "timeline_path": variant.timeline_path,
            "duration_seconds": variant.duration_seconds,
            "status": variant.status,
            "checks_json": variant.checks_json or [],
            "file_url": f"/api/v1/production/jobs/{job.id}/variants/{variant.aspect_ratio}/file",
        }
        for variant in variants
    ]
    caption_track_payload = await _caption_payload(session, job, script)
    return {
        "id": job.id,
        "status": job.status,
        "current_stage": job.current_stage,
        "script_id": job.script_id,
        "article_id": job.article_id,
        "video_render_id": job.video_render_id,
        "style_key": job.style_key,
        "aspect_ratio": job.aspect_ratio,
        "requested_by": job.requested_by,
        "fingerprint": job.fingerprint,
        "config_json": job.config_json or {},
        "warnings": job.warnings or [],
        "error": job.error,
        "logs": job.logs or [],
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "stages": [
            {
                "key": stage.key,
                "label": stage.label,
                "status": stage.status,
                "attempts": stage.attempts,
                "error": stage.error,
                "logs": stage.logs or [],
                "started_at": stage.started_at,
                "finished_at": stage.finished_at,
                "duration_ms": stage.duration_ms,
            }
            for stage in stages
        ],
        "variants": variant_payload,
        "caption_track": caption_track_payload,
        "scenes": scenes,
        "title": script.title if script else None,
        "article_title": article.title if article else None,
        "script_title": script.title if script else None,
        "approval_status": approval.status if approval else None,
        "review_url": f"/api/v1/reviews/{job.video_render_id}" if job.video_render_id else None,
    }

# --------------------------------------------------------------- dashboard helpers


async def list_stories(session: AsyncSession, settings: Settings) -> list[dict[str, Any]]:
    """Return scripts an operator can turn into a video, with production readiness."""

    rows = list(
        (
            await session.execute(
                select(NewsScript, Article, CredibilityAssessment, ResearchBrief)
                .join(Article, Article.id == NewsScript.article_id)
                .outerjoin(
                    CredibilityAssessment,
                    CredibilityAssessment.article_id == Article.id,
                )
                .outerjoin(ResearchBrief, ResearchBrief.article_id == Article.id)
                .order_by(NewsScript.created_at.desc())
                .limit(200)
            )
        )
        .tuples()
        .all()
    )
    stories: list[dict[str, Any]] = []
    for script, article, credibility, brief in rows:
        plan = await session.scalar(
            select(ScenePlan).where(ScenePlan.script_id == script.id)
        )
        narration = await session.scalar(
            select(NarrationAudio).where(NarrationAudio.script_id == script.id)
        )
        render = (
            await session.scalar(
                select(VideoRender).where(
                    VideoRender.narration_audio_id == narration.id
                )
            )
            if narration is not None
            else None
        )
        ready = fallback = 0
        if plan is not None:
            scene_ids = list(
                (
                    await session.scalars(
                        select(Scene.id).where(Scene.scene_plan_id == plan.id)
                    )
                ).all()
            )
            if scene_ids:
                assets = list(
                    (
                        await session.scalars(
                            select(SceneAsset).where(
                                SceneAsset.scene_id.in_(scene_ids)
                            )
                        )
                    ).all()
                )
                ready = sum(1 for row in assets if row.status == "ready")
                fallback = sum(1 for row in assets if row.status == "fallback")
        stories.append(
            {
                "script_id": script.id,
                "article_id": article.id,
                "title": script.title,
                "article_title": article.title,
                "source_name": article.source_name,
                "status": script.status,
                "review_reasons": list(script.review_reasons or []),
                "credibility_score": credibility.confidence_score
                if credibility
                else None,
                "credibility_status": credibility.status if credibility else None,
                "estimated_duration_seconds": script.estimated_duration_seconds,
                "has_scene_plan": plan is not None,
                "has_narration": narration is not None,
                "has_render": render is not None,
                "asset_ready": ready,
                "asset_fallback": fallback,
                "created_at": script.created_at,
                "_brief_sources": brief.source_count if brief else 0,
            }
        )
    minimum = settings.script_minimum_research_sources
    for story in stories:
        blocking: list[str] = []
        if story["status"] != "draft":
            blocking.append(
                f"The script is '{story['status']}' and needs editorial review first."
            )
        if story.pop("_brief_sources", 0) < minimum:
            blocking.append("Insufficient research sources for production.")
        story["production_ready"] = not blocking
        story["blocking_reasons"] = blocking
    return stories


def build_config(settings: Settings) -> dict[str, Any]:
    """Describe every production option the dashboard can offer right now."""

    ffmpeg = resolve_executable(settings.video_ffmpeg_executable)
    piper = resolve_executable(settings.tts_piper_executable)
    piper_ready = bool(piper and settings.tts_piper_voice_model.is_file())
    providers = [
        name.strip()
        for name in settings.asset_provider_priority.split(",")
        if name.strip()
    ]
    if settings.pexels_api_key:
        providers.append("pexels")
    if settings.pixabay_api_key:
        providers.append("pixabay")
    styles = []
    for preset in list_presets():
        craft = preset["craft"]
        styles.append(
            {
                "key": preset["key"],
                "name": preset["name"],
                "description": preset["description"],
                "best_for": preset["best_for"],
                "transition": preset["transition"],
                "motion": preset["motion"],
                "caption_style": preset["caption_style"],
                "graphics": preset["graphics"],
                "asset_priority": preset["asset_priority"],
                "music": preset["music"],
                "craft": craft,
                "landscape": preset["landscape"],
                "portrait": preset["portrait"],
            }
        )
    defaults = load_defaults(settings)
    return {
        "defaults": defaults,
        "styles": styles,
        "asset_providers": providers,
        "asset_strategy": settings.asset_strategy_default,
        "voice_name": settings.tts_piper_voice_model.stem,
        "ffmpeg_available": ffmpeg is not None,
        "piper_available": piper_ready,
        "captions_available": settings.video_captions_enabled,
        "caption_presets": list_caption_presets(),
        "caption_defaults": {
            "preset_key": settings.caption_default_preset,
            "burn_in": settings.caption_burn_in_default,
            "soft_subs": settings.caption_soft_subs_default,
            "sidecar": settings.caption_sidecar_default,
            "transcription_provider": settings.caption_transcription_provider,
        },
        "caption_transcription": transcription_status(),
        "music_dir": str(settings.audio_music_dir),
        "destination_options": list(DESTINATIONS),
        "aspect_options": ["both", "16:9", "9:16"],
        "duration_limits": {
            "min": max(5, settings.scene_min_duration_seconds),
            "max": 600,
            "default": settings.script_target_seconds,
        },
        "licensing_note": (
            "Visuals are retrieved only from providers whose metadata carries a "
            "reusable license (Wikimedia Commons, NASA, and optional Pexels/Pixabay "
            "keys). Each asset is signature-checked, decoded, and stored with its "
            "creator, license, and source URL. Scenes without a validated asset use "
            "a labelled designed graphic instead. Music is never bundled: supply a "
            "track you are licensed to distribute."
        ),
    }


def load_defaults(settings: Settings) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "style_key": settings.video_default_style,
        "aspect_ratio": "both",
        "captions": settings.video_captions_enabled,
        "asset_strategy": settings.asset_strategy_default,
        "voice": settings.tts_piper_voice_model.stem,
        "destination": "local",
    }
    path = settings.production_defaults_file
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            defaults.update(
                {
                    key: value
                    for key, value in stored.items()
                    if key in defaults and value is not None
                }
            )
    return defaults


def save_defaults(settings: Settings, updates: dict[str, Any]) -> dict[str, Any]:
    current = load_defaults(settings)
    for key, value in updates.items():
        if key in current and value is not None:
            current[key] = value
    path = settings.production_defaults_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


async def list_jobs(
    session: AsyncSession, limit: int = 20
) -> list[ProductionJob]:
    return list(
        (
            await session.scalars(
                select(ProductionJob)
                .order_by(ProductionJob.created_at.desc())
                .limit(limit)
            )
        ).all()
    )
