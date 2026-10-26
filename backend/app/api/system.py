from __future__ import annotations

import importlib.util
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.serializers import as_utc
from backend.app.core.config import get_settings
from backend.app.db.models import (
    AgentRun,
    ApprovalEvent,
    Article,
    ArticleEmbedding,
    ContentApproval,
    CredibilityAssessment,
    DuplicateCluster,
    NarrationAudio,
    NewsScript,
    PublishingRecord,
    ResearchBrief,
    ScenePlan,
    VideoRender,
)
from backend.app.schemas.system import (
    AgentRunRead,
    ApprovalEventRead,
    AttentionItemRead,
    OverviewMetricRead,
    OverviewRead,
    PipelineStageRead,
    ServiceHealthRead,
    SystemStatusRead,
)
from backend.app.services.workflow_stages import STAGE_SPECS

router = APIRouter(tags=["system"])


async def get_db_session(request: Request):
    async with request.app.state.session_factory() as session:
        yield session


@router.get("/agent-runs", response_model=list[AgentRunRead])
async def list_agent_runs(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    agent_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AgentRun]:
    statement = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
    if agent_name:
        statement = statement.where(AgentRun.agent_name == agent_name)
    if status:
        statement = statement.where(AgentRun.status == status)
    return list((await session.scalars(statement)).all())


@router.get("/approval-events", response_model=list[ApprovalEventRead])
async def list_approval_events(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    video_render_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ApprovalEvent]:
    statement = select(ApprovalEvent).order_by(ApprovalEvent.created_at.desc()).limit(limit)
    if video_render_id:
        statement = statement.where(ApprovalEvent.video_render_id == video_render_id)
    return list((await session.scalars(statement)).all())


async def _count(session: AsyncSession, statement) -> int:
    return int((await session.scalar(statement)) or 0)


async def _journal(session: AsyncSession) -> dict[str, int]:
    return {
        "articles": await _count(
            session, select(func.count()).select_from(Article)
        ),
        "scripts": await _count(
            session, select(func.count()).select_from(NewsScript)
        ),
        "video_renders": await _count(
            session, select(func.count()).select_from(VideoRender)
        ),
        "pending_approvals": await _count(
            session,
            select(func.count())
            .select_from(ContentApproval)
            .where(ContentApproval.status == "pending"),
        ),
        "publishing_records": await _count(
            session, select(func.count()).select_from(PublishingRecord)
        ),
    }


def _capabilities(settings) -> dict[str, bool]:
    return {
        "video_ffmpeg": _command_available(settings.video_ffmpeg_executable),
        "tts_ffmpeg": _command_available(settings.tts_ffmpeg_executable),
        "piper_tts": _command_available(settings.tts_piper_executable)
        and Path(settings.tts_piper_voice_model).is_file(),
        "local_embeddings": importlib.util.find_spec("sentence_transformers") is not None,
        "local_publishing": True,
        "youtube_publishing": all(
            (
                settings.publishing_youtube_client_id,
                settings.publishing_youtube_client_secret,
                settings.publishing_youtube_refresh_token,
            )
        ),
    }


@router.get("/system/status", response_model=SystemStatusRead)
async def system_status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SystemStatusRead:
    scheduler = getattr(request.app.state, "scheduler", None)
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return SystemStatusRead(
        status="ok",
        scheduler_running=bool(scheduler and scheduler.running),
        capabilities=_capabilities(settings),
        journal=await _journal(session),
    )


@router.get("/system/overview", response_model=OverviewRead)
async def system_overview(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> OverviewRead:
    """A single, real-data snapshot for the editorial command center."""

    now = datetime.now(UTC)
    settings = getattr(request.app.state, "settings", None) or get_settings()
    scheduler = getattr(request.app.state, "scheduler", None)
    coordinator = getattr(request.app.state, "run_coordinator", None)

    cutoff = now - timedelta(hours=24)
    article_without_embedding = ~exists(
        select(ArticleEmbedding.id).where(ArticleEmbedding.article_id == Article.id)
    )
    article_without_assessment = ~exists(
        select(CredibilityAssessment.id).where(
            CredibilityAssessment.article_id == Article.id
        )
    )
    assessed_without_brief = ~exists(
        select(ResearchBrief.id).where(
            ResearchBrief.article_id == CredibilityAssessment.article_id
        )
    )
    assessed_without_script = ~exists(
        select(NewsScript.id).where(
            NewsScript.article_id == CredibilityAssessment.article_id
        )
    )
    script_without_plan = ~exists(
        select(ScenePlan.id).where(ScenePlan.script_id == NewsScript.id)
    )
    plan_without_narration = ~exists(
        select(NarrationAudio.id).where(NarrationAudio.script_id == NewsScript.id)
    )
    narration_without_render = ~exists(
        select(VideoRender.id).where(
            VideoRender.narration_audio_id == NarrationAudio.id
        )
    )

    collected_24h = await _count(
        session,
        select(func.count())
        .select_from(Article)
        .where(Article.collected_at >= cutoff),
    )
    awaiting_dedup = await _count(
        session,
        select(func.count()).select_from(Article).where(article_without_embedding),
    )
    awaiting_verification = await _count(
        session,
        select(func.count()).select_from(Article).where(article_without_assessment),
    )
    awaiting_research = await _count(
        session,
        select(func.count())
        .select_from(CredibilityAssessment)
        .where(
            CredibilityAssessment.status == "corroborated",
            assessed_without_brief,
        ),
    )
    awaiting_script = await _count(
        session,
        select(func.count())
        .select_from(CredibilityAssessment)
        .join(
            ResearchBrief,
            ResearchBrief.article_id == CredibilityAssessment.article_id,
        )
        .where(
            CredibilityAssessment.status == "corroborated",
            ResearchBrief.status == "completed",
            assessed_without_script,
        ),
    )
    awaiting_scenes = await _count(
        session,
        select(func.count())
        .select_from(NewsScript)
        .where(NewsScript.status == "draft", script_without_plan),
    )
    awaiting_tts = await _count(
        session,
        select(func.count())
        .select_from(NewsScript)
        .join(ScenePlan, ScenePlan.script_id == NewsScript.id)
        .where(
            NewsScript.status == "draft",
            ScenePlan.status == "draft",
            plan_without_narration,
        ),
    )
    awaiting_video = await _count(
        session,
        select(func.count())
        .select_from(NarrationAudio)
        .join(NewsScript, NewsScript.id == NarrationAudio.script_id)
        .join(ScenePlan, ScenePlan.id == NarrationAudio.scene_plan_id)
        .where(
            NarrationAudio.status == "completed",
            NewsScript.status == "draft",
            ScenePlan.status == "draft",
            narration_without_render,
        ),
    )
    awaiting_publish = await _count(
        session,
        select(func.count())
        .select_from(PublishingRecord)
        .where(PublishingRecord.status == "queued"),
    )
    pending_approvals = await _count(
        session,
        select(func.count())
        .select_from(ContentApproval)
        .where(ContentApproval.status == "pending"),
    )
    publishing_failures = await _count(
        session,
        select(func.count())
        .select_from(PublishingRecord)
        .where(PublishingRecord.status.in_(("failed", "blocked"))),
    )
    needs_review_claims = await _count(
        session,
        select(func.count())
        .select_from(CredibilityAssessment)
        .where(CredibilityAssessment.status == "needs_review"),
    )
    duplicate_review = await _count(
        session,
        select(func.count())
        .select_from(DuplicateCluster)
        .where(DuplicateCluster.status == "review"),
    )
    active_render_jobs = await _count(
        session,
        select(func.count())
        .select_from(VideoRender)
        .where(VideoRender.status.in_(("queued", "running", "rendering"))),
    )
    draft_renders = await _count(
        session,
        select(func.count())
        .select_from(VideoRender)
        .where(VideoRender.status == "draft"),
    )
    active_runs = await _count(
        session,
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.status == "running"),
    )
    last_sync = await session.scalar(
        select(func.max(AgentRun.finished_at)).where(
            AgentRun.agent_name == "news_collector",
            AgentRun.status.in_(("completed", "completed_with_errors")),
        )
    )

    recent_runs = list(
        (
            await session.scalars(
                select(AgentRun).order_by(AgentRun.started_at.desc()).limit(8)
            )
        ).all()
    )
    last_by_agent: dict[str, AgentRun] = {}
    for run in (
        await session.scalars(
            select(AgentRun).order_by(AgentRun.started_at.desc()).limit(200)
        )
    ).all():
        last_by_agent.setdefault(run.agent_name, run)

    stage_counts = {
        "collection": collected_24h,
        "deduplication": awaiting_dedup,
        "credibility": awaiting_verification,
        "research": awaiting_research,
        "script": awaiting_script,
        "scenes": awaiting_scenes,
        "tts": awaiting_tts,
        "video": awaiting_video,
        "publishing": awaiting_publish,
    }
    count_labels = {
        "collection": "new in the last 24 hours",
        "deduplication": "awaiting clustering",
        "credibility": "awaiting verification",
        "research": "awaiting research",
        "script": "awaiting a script",
        "scenes": "awaiting a scene plan",
        "tts": "awaiting narration",
        "video": "awaiting a render",
        "publishing": "queued to publish",
    }
    pipeline: list[PipelineStageRead] = []
    for spec in STAGE_SPECS:
        last_run = last_by_agent.get(spec.agent_name)
        is_running = bool(
            coordinator is not None and coordinator.is_running(spec.agent_name)
        )
        if is_running or (last_run is not None and last_run.status == "running"):
            status = "running" if is_running else "stalled"
            status_label = "Running" if is_running else "Interrupted"
        elif last_run is not None and last_run.status == "failed":
            status = "failed"
            status_label = "Failed"
        elif last_run is not None and last_run.status == "completed_with_errors":
            status = "warning"
            status_label = "Completed with warnings"
        elif stage_counts[spec.key] > 0:
            status = "ready"
            status_label = "Ready"
        elif last_run is not None:
            status = "completed"
            status_label = "Complete"
        else:
            status = "idle"
            status_label = "Not run yet"
        pipeline.append(
            PipelineStageRead(
                key=spec.key,
                label=spec.label,
                description=spec.description,
                agent_name=spec.agent_name,
                status=status,
                status_label=status_label,
                count=stage_counts[spec.key],
                count_label=count_labels[spec.key],
                action="trigger",
                action_path="/api/v1/workflow/runs",
                last_run=AgentRunRead.model_validate(last_run) if last_run else None,
            )
        )

    attention: list[AttentionItemRead] = []
    # Only surface a failure while it is the latest run for that agent. An older
    # failure that has since been retried successfully is history, not attention.
    latest_failures = sorted(
        (
            run
            for run in last_by_agent.values()
            if run.status in {"failed", "completed_with_errors"}
        ),
        key=lambda run: run.finished_at or run.started_at,
        reverse=True,
    )
    for run in latest_failures:
        attention.append(
            AttentionItemRead(
                id=f"run-{run.id}",
                kind="run_failed",
                severity="error" if run.status == "failed" else "warning",
                title=f"{_agent_label(run.agent_name)} run {run.status.replace('_', ' ')}",
                detail=(run.errors or ["No error detail was recorded."])[-1],
                href="#/runs",
                created_at=as_utc(run.finished_at or run.started_at),
            )
        )
    if pending_approvals:
        attention.append(
            AttentionItemRead(
                id="approval-pending",
                kind="approval_pending",
                severity="warning",
                title=(
                    f"{pending_approvals} draft"
                    f"{'s' if pending_approvals != 1 else ''} awaiting editorial approval"
                ),
                detail="Review the script, sources, scenes, and quality gates before publishing.",
                href="#/review",
                created_at=now,
            )
        )
    if needs_review_claims:
        attention.append(
            AttentionItemRead(
                id="verification-review",
                kind="verification_review",
                severity="warning",
                title=(
                    f"{needs_review_claims} stor"
                    f"{'ies' if needs_review_claims != 1 else 'y'} "
                    "flagged for verification review"
                ),
                detail="Corroboration did not meet the configured confidence threshold.",
                href="#/articles",
                created_at=now,
            )
        )
    if duplicate_review:
        attention.append(
            AttentionItemRead(
                id="duplicate-review",
                kind="duplicate_review",
                severity="info",
                title=(
                    f"{duplicate_review} duplicate cluster"
                    f"{'s' if duplicate_review != 1 else ''} need review"
                ),
                detail="Confirm or reject possible syndicated coverage before it enters research.",
                href="#/articles",
                created_at=now,
            )
        )
    if publishing_failures:
        failed_records = list(
            (
                await session.scalars(
                    select(PublishingRecord)
                    .where(PublishingRecord.status.in_(("failed", "blocked")))
                    .order_by(PublishingRecord.updated_at.desc())
                    .limit(5)
                )
            ).all()
        )
        for record in failed_records:
            attention.append(
                AttentionItemRead(
                    id=f"publishing-{record.id}",
                    kind="publishing_failed",
                    severity="error",
                    title=f"Publishing {record.status}: {record.platform}",
                    detail=record.last_error or "No error detail was recorded.",
                    href="#/publishing",
                    created_at=as_utc(record.updated_at),
                )
            )

    metrics = [
        OverviewMetricRead(
            key="collected",
            label="Collected",
            value=collected_24h,
            detail="Articles in the last 24 hours",
        ),
        OverviewMetricRead(
            key="verification",
            label="Awaiting verification",
            value=awaiting_verification,
            detail="Articles without a credibility assessment",
        ),
        OverviewMetricRead(
            key="approvals",
            label="Awaiting approval",
            value=pending_approvals,
            detail="Rendered drafts with a pending human decision",
        ),
        OverviewMetricRead(
            key="renders",
            label="Active renders",
            value=active_render_jobs,
            detail=(
                f"{draft_renders} draft render"
                f"{'s' if draft_renders != 1 else ''} awaiting review"
                if draft_renders
                else "No draft renders awaiting review"
            ),
        ),
        OverviewMetricRead(
            key="publishing_attention",
            label="Publishing issues",
            value=publishing_failures,
            detail="Failed or blocked publishing records",
        ),
    ]

    services = _service_health(settings)
    status = "ok" if all(item.ready for item in services if item.required) else "degraded"

    return OverviewRead(
        generated_at=now,
        status=status,
        scheduler_running=bool(scheduler and scheduler.running),
        last_successful_sync=as_utc(last_sync),
        active_runs=active_runs,
        metrics=metrics,
        pipeline=pipeline,
        attention=attention[:8],
        recent_runs=[AgentRunRead.model_validate(run) for run in recent_runs],
        services=services,
        journal=await _journal(session),
    )


def _service_health(settings) -> list[ServiceHealthRead]:
    capabilities = _capabilities(settings)
    definitions = [
        ("video_ffmpeg", "FFmpeg (video)", settings.video_enabled, "Video rendering"),
        ("tts_ffmpeg", "FFmpeg (audio)", False, "MP3 narration conversion"),
        (
            "piper_tts",
            "Piper TTS",
            settings.tts_enabled and settings.tts_provider == "piper",
            "Local narration synthesis",
        ),
        (
            "local_embeddings",
            "Local embeddings",
            settings.deduplication_enabled,
            "Duplicate clustering",
        ),
        ("local_publishing", "Local publishing", True, "Export approved renders"),
        ("youtube_publishing", "YouTube credentials", False, "External publishing"),
    ]
    return [
        ServiceHealthRead(
            key=key,
            label=label,
            ready=capabilities[key],
            required=required,
            detail=(
                f"{purpose} is configured."
                if capabilities[key]
                else f"{purpose} is not configured."
            ),
        )
        for key, label, required, purpose in definitions
    ]


def _agent_label(agent_name: str) -> str:
    for spec in STAGE_SPECS:
        if spec.agent_name == agent_name:
            return spec.label
    return agent_name.replace("_", " ").title()


def _command_available(command: str) -> bool:
    if not command:
        return False
    return shutil.which(command) is not None or Path(command).is_file()
