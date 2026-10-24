from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import (
    Article,
    ContentApproval,
    CredibilityAssessment,
    CredibilityEvidence,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    PublishingRecord,
    ResearchBrief,
    ResearchSource,
    Scene,
    ScenePlan,
    VideoRender,
)
from backend.app.schemas.article import ArticleRead
from backend.app.schemas.credibility import CredibilityAssessmentRead, CredibilityEvidenceRead
from backend.app.schemas.publishing import PublishingRecordRead
from backend.app.schemas.research import ResearchBriefRead, ResearchSourceRead
from backend.app.schemas.review import ApprovalRead, ReviewSummary
from backend.app.schemas.scene import ScenePlanRead, SceneRead
from backend.app.schemas.script import NewsScriptRead
from backend.app.schemas.tts import NarrationAudioRead, NarrationChunkRead
from backend.app.schemas.video import VideoRenderRead


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def approval_read(approval: ContentApproval) -> ApprovalRead:
    return ApprovalRead(
        id=approval.id,
        video_render_id=approval.video_render_id,
        status=approval.status,
        reviewer=approval.reviewer,
        notes=approval.notes,
        quality_checks=approval.quality_checks or [],
        artifact_sha256=approval.artifact_sha256,
        decided_at=approval.decided_at,
        created_at=approval.created_at,
        updated_at=approval.updated_at,
    )


async def load_credibility(
    session: AsyncSession, article_id: str
) -> CredibilityAssessmentRead | None:
    assessment = await session.scalar(
        select(CredibilityAssessment).where(CredibilityAssessment.article_id == article_id)
    )
    if assessment is None:
        return None
    evidence = list(
        (
            await session.scalars(
                select(CredibilityEvidence)
                .where(CredibilityEvidence.assessment_id == assessment.id)
                .order_by(CredibilityEvidence.relevance_score.desc())
            )
        ).all()
    )
    return CredibilityAssessmentRead(
        id=assessment.id,
        article_id=assessment.article_id,
        status=assessment.status,
        confidence_score=assessment.confidence_score,
        explanation=assessment.explanation,
        claims=assessment.claims,
        independent_source_count=assessment.independent_source_count,
        extractor_name=assessment.extractor_name,
        created_at=assessment.created_at,
        updated_at=assessment.updated_at,
        evidence=[CredibilityEvidenceRead.model_validate(item) for item in evidence],
    )


async def load_research(session: AsyncSession, article_id: str) -> ResearchBriefRead | None:
    brief = await session.scalar(
        select(ResearchBrief).where(ResearchBrief.article_id == article_id)
    )
    if brief is None:
        return None
    sources = list(
        (
            await session.scalars(
                select(ResearchSource)
                .where(ResearchSource.brief_id == brief.id)
                .order_by(ResearchSource.relevance_score.desc())
            )
        ).all()
    )
    return ResearchBriefRead(
        id=brief.id,
        article_id=brief.article_id,
        status=brief.status,
        headline=brief.headline,
        key_facts=brief.key_facts,
        context=brief.context,
        uncertainties=brief.uncertainties,
        source_count=brief.source_count,
        summarizer_name=brief.summarizer_name,
        created_at=brief.created_at,
        updated_at=brief.updated_at,
        sources=[ResearchSourceRead.model_validate(source) for source in sources],
    )


async def load_scene_plan(session: AsyncSession, script_id: str) -> ScenePlanRead | None:
    plan = await session.scalar(select(ScenePlan).where(ScenePlan.script_id == script_id))
    if plan is None:
        return None
    scenes = list(
        (
            await session.scalars(
                select(Scene).where(Scene.scene_plan_id == plan.id).order_by(Scene.position.asc())
            )
        ).all()
    )
    return ScenePlanRead(
        id=plan.id,
        script_id=plan.script_id,
        status=plan.status,
        total_duration_seconds=plan.total_duration_seconds,
        planner_name=plan.planner_name,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        scenes=[SceneRead.model_validate(scene) for scene in scenes],
    )


async def load_narration(session: AsyncSession, script_id: str) -> NarrationAudioRead | None:
    narration = await session.scalar(
        select(NarrationAudio).where(NarrationAudio.script_id == script_id)
    )
    if narration is None:
        return None
    chunks = list(
        (
            await session.scalars(
                select(NarrationChunk)
                .where(NarrationChunk.narration_audio_id == narration.id)
                .order_by(NarrationChunk.position.asc())
            )
        ).all()
    )
    return NarrationAudioRead(
        id=narration.id,
        script_id=narration.script_id,
        scene_plan_id=narration.scene_plan_id,
        status=narration.status,
        provider_name=narration.provider_name,
        voice_name=narration.voice_name,
        wav_path=narration.wav_path,
        mp3_path=narration.mp3_path,
        duration_seconds=narration.duration_seconds,
        sample_rate=narration.sample_rate,
        created_at=narration.created_at,
        updated_at=narration.updated_at,
        chunks=[NarrationChunkRead.model_validate(chunk) for chunk in chunks],
    )


async def load_publishing_records(
    session: AsyncSession, video_render_id: str
) -> list[PublishingRecordRead]:
    records = list(
        (
            await session.scalars(
                select(PublishingRecord)
                .where(PublishingRecord.video_render_id == video_render_id)
                .order_by(PublishingRecord.created_at.desc())
            )
        ).all()
    )
    return [PublishingRecordRead.model_validate(record) for record in records]


def priority_score(
    *,
    credibility_score: float | None,
    trust_weight: float,
    published_at: datetime | None,
    collected_at: datetime,
    approval_status: str,
    now: datetime | None = None,
) -> float:
    """Rank the review queue without pretending that a score proves newsworthiness."""

    reference = as_utc(published_at) or as_utc(collected_at) or datetime.now(UTC)
    current = now or datetime.now(UTC)
    age_hours = max(0.0, (current - reference).total_seconds() / 3_600)
    recency = max(0.0, 1.0 - age_hours / (24 * 7))
    review_bonus = 1.0 if approval_status == "pending" else 0.5
    score = (
        45 * (credibility_score or 0.0)
        + 25 * trust_weight
        + 15 * recency
        + 15 * review_bonus
    )
    return round(min(100.0, max(0.0, score)), 1)


def review_summary(
    *,
    render: VideoRender,
    script: NewsScript,
    article: Article,
    credibility: CredibilityAssessment | None,
    approval: ContentApproval | None,
    trust_weight: float,
    publish_status: str | None,
) -> ReviewSummary:
    approval_status = approval.status if approval else "pending"
    return ReviewSummary(
        video_render_id=render.id,
        script_id=script.id,
        article_id=article.id,
        title=script.title,
        article_title=article.title,
        source_name=article.source_name,
        category=article.category,
        country=article.country,
        credibility_status=credibility.status if credibility else None,
        credibility_score=credibility.confidence_score if credibility else None,
        approval_status=approval_status,
        video_status=render.status,
        duration_seconds=render.duration_seconds,
        created_at=render.created_at,
        priority_score=priority_score(
            credibility_score=credibility.confidence_score if credibility else None,
            trust_weight=trust_weight,
            published_at=article.published_at,
            collected_at=article.collected_at,
            approval_status=approval_status,
        ),
        publish_status=publish_status,
    )


def video_read(render: VideoRender) -> VideoRenderRead:
    return VideoRenderRead.model_validate(render)


def article_read(article: Article) -> ArticleRead:
    return ArticleRead.model_validate(article)


def script_read(script: NewsScript) -> NewsScriptRead:
    return NewsScriptRead.model_validate(script)


def records_metadata(records: list[PublishingRecordRead]) -> dict[str, Any]:
    return {
        "latest_status": records[0].status if records else None,
        "count": len(records),
    }
