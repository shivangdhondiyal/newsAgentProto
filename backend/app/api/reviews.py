from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.api.serializers import (
    approval_read,
    article_read,
    as_utc,
    load_credibility,
    load_narration,
    load_publishing_records,
    load_research,
    load_scene_plan,
    review_summary,
    script_read,
    video_read,
)
from backend.app.core.config import get_settings
from backend.app.db.models import (
    Article,
    ContentApproval,
    CredibilityAssessment,
    NarrationAudio,
    NewsScript,
    PublishingRecord,
    ResearchBrief,
    Scene,
    ScenePlan,
    VideoRender,
)
from backend.app.reviews.quality import evaluate_video_quality, sha256_file
from backend.app.reviews.service import (
    ApprovalBlockedError,
    decide_approval,
    get_or_create_approval,
    record_event,
    remove_script_generation,
    remove_video_generation,
)
from backend.app.schemas.publishing import ReviewAnalyticsRead
from backend.app.schemas.review import (
    ApprovalRead,
    ContentEditResult,
    ReviewDecision,
    ReviewDetail,
    ReviewSummary,
    SceneUpdate,
    ScriptUpdate,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])
SENSATIONAL_TERMS = {"breaking", "shocking", "outrageous", "must-see", "unbelievable"}


async def get_db_session(request: Request):
    async with request.app.state.session_factory() as session:
        yield session


def _trust_weight(request: Request, source_id: str) -> float:
    policy: SourceTrustPolicy | None = getattr(request.app.state, "trust_policy", None)
    if policy is None:
        policy = SourceTrustPolicy.from_file(get_settings().trust_policy_file)
    return policy.weight_for(source_id)


async def _load_render(
    session: AsyncSession, video_render_id: str
) -> tuple[VideoRender, NarrationAudio, NewsScript, Article, ScenePlan] | None:
    row = (
        (
            await session.execute(
                select(VideoRender, NarrationAudio, NewsScript, Article, ScenePlan)
                .join(NarrationAudio, NarrationAudio.id == VideoRender.narration_audio_id)
                .join(ScenePlan, ScenePlan.id == VideoRender.scene_plan_id)
                .join(NewsScript, NewsScript.id == NarrationAudio.script_id)
                .join(Article, Article.id == NewsScript.article_id)
                .where(VideoRender.id == video_render_id)
            )
        )
        .tuples()
        .first()
    )
    return row  # type: ignore[return-value]


async def _publishing_records(
    session: AsyncSession, video_render_id: str
) -> list[PublishingRecord]:
    return list(
        (
            await session.scalars(
                select(PublishingRecord)
                .where(PublishingRecord.video_render_id == video_render_id)
                .order_by(PublishingRecord.created_at.desc())
            )
        ).all()
    )


@router.get("", response_model=list[ReviewSummary])
async def list_reviews(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    review_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ReviewSummary]:
    rows = list(
        (
            await session.execute(
                select(
                    VideoRender,
                    NarrationAudio,
                    NewsScript,
                    Article,
                    ScenePlan,
                    CredibilityAssessment,
                )
                .join(NarrationAudio, NarrationAudio.id == VideoRender.narration_audio_id)
                .join(ScenePlan, ScenePlan.id == VideoRender.scene_plan_id)
                .join(NewsScript, NewsScript.id == NarrationAudio.script_id)
                .join(Article, Article.id == NewsScript.article_id)
                .outerjoin(CredibilityAssessment, CredibilityAssessment.article_id == Article.id)
                .order_by(VideoRender.created_at.desc())
                .limit(500)
            )
        )
        .tuples()
        .all()
    )
    render_ids = [row[0].id for row in rows]
    approvals = {
        approval.video_render_id: approval
        for approval in (
            await session.scalars(
                select(ContentApproval).where(ContentApproval.video_render_id.in_(render_ids))
            )
        ).all()
    }
    publish_status: dict[str, str] = {}
    if render_ids:
        records = list(
            (
                await session.scalars(
                    select(PublishingRecord)
                    .where(PublishingRecord.video_render_id.in_(render_ids))
                    .order_by(PublishingRecord.created_at.asc())
                )
            ).all()
        )
        for record in records:
            publish_status[record.video_render_id] = record.status

    summaries: list[ReviewSummary] = []
    for render, _narration, script, article, _plan, credibility in rows:
        approval = approvals.get(render.id)
        approval_status = approval.status if approval else "pending"
        if review_status and approval_status != review_status:
            continue
        summaries.append(
            review_summary(
                render=render,
                script=script,
                article=article,
                credibility=credibility,
                approval=approval,
                trust_weight=_trust_weight(request, article.source_id),
                publish_status=publish_status.get(render.id),
            )
        )
    summaries.sort(key=lambda item: (item.priority_score, as_utc(item.created_at)), reverse=True)
    return summaries[:limit]


@router.get("/analytics", response_model=ReviewAnalyticsRead)
async def review_analytics(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ReviewAnalyticsRead:
    approval_counts = {
        status: count
        for status, count in (
            await session.execute(
                select(ContentApproval.status, func.count()).group_by(ContentApproval.status)
            )
        ).all()
    }
    publishing_counts = {
        status: count
        for status, count in (
            await session.execute(
                select(PublishingRecord.status, func.count()).group_by(PublishingRecord.status)
            )
        ).all()
    }
    return ReviewAnalyticsRead(
        pending=approval_counts.get("pending", 0),
        approved=approval_counts.get("approved", 0),
        rejected=approval_counts.get("rejected", 0),
        changes_requested=approval_counts.get("changes_requested", 0),
        published=publishing_counts.get("published", 0),
        exported=publishing_counts.get("exported", 0),
    )


@router.get("/{video_render_id}", response_model=ReviewDetail)
async def get_review(
    request: Request,
    video_render_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ReviewDetail:
    row = await _load_render(session, video_render_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Video render not found")
    render, narration, script, article, _plan = row
    settings = get_settings()
    approval = await get_or_create_approval(session, render, script, settings)
    if approval.status == "pending":
        approval.quality_checks = evaluate_video_quality(
            render, script, settings.approval_min_video_bytes
        )
        approval.artifact_sha256 = sha256_file(Path(render.video_path))
    await session.commit()
    await session.refresh(approval)

    credibility = await load_credibility(session, article.id)
    research = await load_research(session, article.id)
    scene_plan = await load_scene_plan(session, script.id)
    narration_read = await load_narration(session, script.id)
    if scene_plan is None or narration_read is None:
        raise HTTPException(status_code=409, detail="Review media provenance is incomplete")
    records = await load_publishing_records(session, render.id)
    summary = review_summary(
        render=render,
        script=script,
        article=article,
        credibility=await session.scalar(
            select(CredibilityAssessment).where(CredibilityAssessment.article_id == article.id)
        ),
        approval=approval,
        trust_weight=_trust_weight(request, article.source_id),
        publish_status=records[0].status if records else None,
    )
    return ReviewDetail(
        summary=summary,
        video=video_read(render),
        article=article_read(article),
        credibility=credibility,
        research=research,
        script=script_read(script),
        scene_plan=scene_plan,
        narration=narration_read,
        approval=approval_read(approval),
        quality_checks=approval.quality_checks or [],
        publishing_records=records,
    )


@router.post("/{video_render_id}/decision", response_model=ApprovalRead)
async def record_decision(
    video_render_id: str,
    decision: ReviewDecision,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApprovalRead:
    row = await _load_render(session, video_render_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Video render not found")
    render, _narration, script, _article, _plan = row
    records = await _publishing_records(session, render.id)
    if records:
        raise HTTPException(
            status_code=409,
            detail=(
                "This content already has publishing activity; approval is locked so a "
                "queued or published artifact cannot be re-approved after modification"
            ),
        )
    approval = await get_or_create_approval(session, render, script, get_settings())
    try:
        await decide_approval(
            session,
            render=render,
            script=script,
            approval=approval,
            action=decision.action,
            reviewer=decision.reviewer,
            notes=decision.notes,
            settings=get_settings(),
        )
    except ApprovalBlockedError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Deterministic video quality checks failed; approval was not recorded.",
                "checks": exc.checks,
            },
        ) from exc
    await session.commit()
    await session.refresh(approval)
    return approval_read(approval)


@router.patch("/scripts/{script_id}", response_model=ContentEditResult)
async def edit_script(
    script_id: str,
    update: ScriptUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ContentEditResult:
    script = await session.get(NewsScript, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    narration = await session.scalar(
        select(NarrationAudio).where(NarrationAudio.script_id == script.id)
    )
    plan = await session.scalar(select(ScenePlan).where(ScenePlan.script_id == script.id))
    has_downstream = narration is not None or plan is not None
    publishing_records = list(
        (
            await session.scalars(
                select(PublishingRecord)
                .join(VideoRender, VideoRender.id == PublishingRecord.video_render_id)
                .join(
                    NarrationAudio,
                    NarrationAudio.id == VideoRender.narration_audio_id,
                )
                .where(NarrationAudio.script_id == script.id)
            )
        ).all()
    )
    if publishing_records:
        raise HTTPException(
            status_code=409,
            detail="Scripts with publishing activity cannot be edited; create a new story instead",
        )
    if has_downstream and not update.acknowledge_media_invalidation:
        raise HTTPException(
            status_code=409,
            detail=(
                "Editing this script invalidates generated audio, video, and approval. "
                "Set acknowledge_media_invalidation=true to proceed."
            ),
        )

    if update.main_story is not None and not any(item.strip() for item in update.main_story):
        raise HTTPException(status_code=422, detail="main_story cannot be empty")
    invalidated = has_downstream
    await remove_script_generation(
        session,
        script=script,
        settings=get_settings(),
        actor=update.actor,
        notes="Script edited from the review dashboard.",
    )

    for field in ("title", "hook", "introduction", "main_story", "context", "closing"):
        value = getattr(update, field)
        if value is not None:
            setattr(script, field, value)
    script.full_text = _render_script(script)
    script.estimated_duration_seconds = _estimate_duration(script.full_text)
    brief = await session.get(ResearchBrief, script.research_brief_id)
    script.review_reasons = _review_reasons(script, brief)
    script.status = "needs_review" if script.review_reasons else "draft"
    await session.commit()
    return ContentEditResult(
        message=(
            "Script saved. Generated media and approval were invalidated; rerun the pipeline."
            if invalidated
            else "Script saved. No generated media needed invalidation."
        ),
        invalidated=invalidated,
    )


@router.patch("/scenes/{scene_id}", response_model=ContentEditResult)
async def edit_scene(
    scene_id: str,
    update: SceneUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ContentEditResult:
    scene = await session.get(Scene, scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    plan = await session.get(ScenePlan, scene.scene_plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Scene plan not found")
    script = await session.get(NewsScript, plan.script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="Source script not found")
    renders = list(
        (
            await session.scalars(
                select(VideoRender)
                .join(NarrationAudio, NarrationAudio.id == VideoRender.narration_audio_id)
                .where(NarrationAudio.script_id == script.id)
            )
        ).all()
    )
    publishing_records = list(
        (
            await session.scalars(
                select(PublishingRecord).where(
                    PublishingRecord.video_render_id.in_([render.id for render in renders])
                )
            )
        ).all()
        if renders
        else []
    )
    if publishing_records:
        raise HTTPException(
            status_code=409,
            detail="Scenes with publishing activity cannot be edited",
        )
    if renders and not update.acknowledge_media_invalidation:
        raise HTTPException(
            status_code=409,
            detail=(
                "Editing scenes invalidates the current video and approval. "
                "Set acknowledge_media_invalidation=true to proceed."
            ),
        )
    invalidated = bool(renders)
    if renders:
        for render in renders:
            await remove_video_generation(
                session,
                render=render,
                settings=get_settings(),
                actor=update.actor,
                notes="Scene plan edited from the review dashboard.",
                action="scene_edited",
                script_id=script.id,
            )
    else:
        record_event(
            session,
            video_render_id=None,
            script_id=script.id,
            action="scene_edited",
            actor=update.actor,
            notes="Scene plan edited from the review dashboard.",
        )

    for field in (
        "subtitle",
        "keywords",
        "scene_description",
        "stock_footage_query",
        "image_search_query",
        "animation_suggestion",
        "transition",
    ):
        value = getattr(update, field)
        if value is not None:
            setattr(scene, field, value)
    await session.commit()
    return ContentEditResult(
        message=(
            "Scene saved. The previous video and approval were invalidated; rerun video generation."
            if invalidated
            else "Scene saved."
        ),
        invalidated=invalidated,
    )


@router.get("/videos/{video_render_id}/file", include_in_schema=False)
async def stream_video(
    video_render_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FileResponse:
    render = await session.get(VideoRender, video_render_id)
    if render is None:
        raise HTTPException(status_code=404, detail="Video render not found")
    path = Path(render.video_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Rendered video file is missing")
    return FileResponse(path, media_type="video/mp4", filename=f"{render.id}.mp4")


def _render_script(script: NewsScript) -> str:
    parts = [
        script.hook,
        script.introduction,
        *script.main_story,
        *script.context,
        script.closing,
    ]
    return "\n\n".join(" ".join(part.split()) for part in parts if part.strip())


def _estimate_duration(text: str) -> int:
    word_count = len(re.findall(r"\b\w+\b", text))
    return max(1, round(word_count / 2.5))


def _review_reasons(script: NewsScript, brief: ResearchBrief | None) -> list[str]:
    reasons: list[str] = []
    if brief is None or brief.source_count < get_settings().script_minimum_research_sources:
        reasons.append("Insufficient research sources for the script.")
    content = _render_script(script).lower()
    prohibited = sorted(term for term in SENSATIONAL_TERMS if term in content)
    if prohibited:
        reasons.append(f"Potentially sensational language found: {', '.join(prohibited)}.")
    if not script.main_story:
        reasons.append("The script contains no evidence-backed main-story section.")
    return reasons
