from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class Article(Base):
    """The canonical normalized input contract for all downstream agents."""

    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("canonical_url", name="uq_articles_canonical_url"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    source_name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentRun(Base):
    """A structured, queryable audit record for an agent execution."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_name: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_created: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ArticleEmbedding(Base):
    """A versioned local embedding. JSON keeps SQLite simple; pgvector is a future adapter."""

    __tablename__ = "article_embeddings"
    __table_args__ = (
        UniqueConstraint("article_id", "model_name", name="uq_article_embeddings_article_model"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    model_name: Mapped[str] = mapped_column(String(255), index=True)
    text_hash: Mapped[str] = mapped_column(String(64))
    vector: Mapped[list[float]] = mapped_column(JSON)
    dimensions: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DuplicateCluster(Base):
    """A duplicate-reporting group; originals remain in articles as provenance evidence."""

    __tablename__ = "duplicate_clusters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    canonical_article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="review", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DuplicateClusterMember(Base):
    """Similarity evidence for an article's membership in a duplicate cluster."""

    __tablename__ = "duplicate_cluster_members"
    __table_args__ = (
        UniqueConstraint("cluster_id", "article_id", name="uq_duplicate_cluster_membership"),
        UniqueConstraint("article_id", name="uq_duplicate_article_cluster"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_id: Mapped[str] = mapped_column(
        ForeignKey("duplicate_clusters.id", ondelete="CASCADE"), index=True
    )
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    similarity: Mapped[float] = mapped_column(Float)
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CredibilityAssessment(Base):
    """A transparent corroboration estimate, distinct from a claim of factual certainty."""

    __tablename__ = "credibility_assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(30), index=True)
    confidence_score: Mapped[float] = mapped_column(Float)
    explanation: Mapped[str] = mapped_column(Text)
    claims: Mapped[list[str]] = mapped_column(JSON, default=list)
    independent_source_count: Mapped[int] = mapped_column(Integer, default=0)
    extractor_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CredibilityEvidence(Base):
    """The evidence used for an assessment, stored for review and reproducibility."""

    __tablename__ = "credibility_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    assessment_id: Mapped[str] = mapped_column(
        ForeignKey("credibility_assessments.id", ondelete="CASCADE"), index=True
    )
    claim: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    publisher: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_score: Mapped[float] = mapped_column(Float)
    trust_weight: Mapped[float] = mapped_column(Float)
    finding: Mapped[str] = mapped_column(String(30), default="candidate_corroboration")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchBrief(Base):
    """An opinion-free contextual briefing built after credibility corroboration."""

    __tablename__ = "research_briefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(30), index=True)
    headline: Mapped[str] = mapped_column(Text)
    key_facts: Mapped[list[str]] = mapped_column(JSON, default=list)
    context: Mapped[list[str]] = mapped_column(JSON, default=list)
    uncertainties: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    summarizer_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ResearchSource(Base):
    """A source preserved in a research brief for editorial inspection."""

    __tablename__ = "research_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    brief_id: Mapped[str] = mapped_column(
        ForeignKey("research_briefs.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(150), index=True)
    publisher: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsScript(Base):
    """Neutral narration draft tied to its corroborated article and research evidence."""

    __tablename__ = "news_scripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), unique=True, index=True
    )
    research_brief_id: Mapped[str] = mapped_column(
        ForeignKey("research_briefs.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(Text)
    hook: Mapped[str] = mapped_column(Text)
    introduction: Mapped[str] = mapped_column(Text)
    main_story: Mapped[list[str]] = mapped_column(JSON, default=list)
    context: Mapped[list[str]] = mapped_column(JSON, default=list)
    closing: Mapped[str] = mapped_column(Text)
    full_text: Mapped[str] = mapped_column(Text)
    estimated_duration_seconds: Mapped[int] = mapped_column(Integer)
    target_duration_seconds: Mapped[int] = mapped_column(Integer)
    writer_name: Mapped[str] = mapped_column(String(100))
    review_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScenePlan(Base):
    """Editable visual plan derived from an eligible script before media generation."""

    __tablename__ = "scene_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    script_id: Mapped[str] = mapped_column(
        ForeignKey("news_scripts.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), index=True)
    total_duration_seconds: Mapped[int] = mapped_column(Integer)
    planner_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Scene(Base):
    """A single editable timeline unit and its visual-search guidance."""

    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("scene_plan_id", "position", name="uq_scene_plan_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    scene_plan_id: Mapped[str] = mapped_column(
        ForeignKey("scene_plans.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    start_seconds: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[int] = mapped_column(Integer)
    narration: Mapped[str] = mapped_column(Text)
    subtitle: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    scene_description: Mapped[str] = mapped_column(Text)
    stock_footage_query: Mapped[str] = mapped_column(Text)
    image_search_query: Mapped[str] = mapped_column(Text)
    animation_suggestion: Mapped[str] = mapped_column(Text)
    transition: Mapped[str] = mapped_column(String(80), default="crossfade")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NarrationAudio(Base):
    """Locally generated narration outputs and their source script/scene-plan provenance."""

    __tablename__ = "narration_audio"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    script_id: Mapped[str] = mapped_column(
        ForeignKey("news_scripts.id", ondelete="CASCADE"), unique=True, index=True
    )
    scene_plan_id: Mapped[str] = mapped_column(
        ForeignKey("scene_plans.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), index=True)
    provider_name: Mapped[str] = mapped_column(String(100))
    voice_name: Mapped[str] = mapped_column(String(255))
    wav_path: Mapped[str] = mapped_column(Text)
    mp3_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float)
    sample_rate: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NarrationChunk(Base):
    """Chunk-level files retain text-to-audio timing for later subtitle/video synchronization."""

    __tablename__ = "narration_chunks"
    __table_args__ = (
        UniqueConstraint("narration_audio_id", "position", name="uq_narration_chunk_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    narration_audio_id: Mapped[str] = mapped_column(
        ForeignKey("narration_audio.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    wav_path: Mapped[str] = mapped_column(Text)
    start_seconds: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VideoRender(Base):
    """A draft local video render awaiting manual approval before any publishing action."""

    __tablename__ = "video_renders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    narration_audio_id: Mapped[str] = mapped_column(
        ForeignKey("narration_audio.id", ondelete="CASCADE"), unique=True, index=True
    )
    scene_plan_id: Mapped[str] = mapped_column(
        ForeignKey("scene_plans.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), index=True)
    renderer_name: Mapped[str] = mapped_column(String(100))
    video_path: Mapped[str] = mapped_column(Text)
    subtitle_path: Mapped[str] = mapped_column(Text)
    timeline_path: Mapped[str] = mapped_column(Text)
    duration_seconds: Mapped[float] = mapped_column(Float)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    fps: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContentApproval(Base):
    """An explicit human decision about one immutable video-render artifact."""

    __tablename__ = "content_approvals"
    __table_args__ = (
        UniqueConstraint("video_render_id", name="uq_content_approvals_video_render"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_render_id: Mapped[str] = mapped_column(
        ForeignKey("video_renders.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    reviewer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApprovalEvent(Base):
    """Append-only editorial audit history; survives regeneration of media records."""

    __tablename__ = "approval_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_render_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    script_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(50), index=True)
    actor: Mapped[str] = mapped_column(String(120), default="operator")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublishingRecord(Base):
    """A durable, idempotent publishing attempt, separate from editorial approval."""

    __tablename__ = "publishing_records"
    __table_args__ = (
        UniqueConstraint(
            "video_render_id", "platform", name="uq_publishing_records_video_platform"
        ),
        UniqueConstraint("idempotency_key", name="uq_publishing_records_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_render_id: Mapped[str] = mapped_column(
        ForeignKey("video_renders.id", ondelete="CASCADE"), index=True
    )
    approval_id: Mapped[str] = mapped_column(
        ForeignKey("content_approvals.id", ondelete="RESTRICT"), index=True
    )
    platform: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
