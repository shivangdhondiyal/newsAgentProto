from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.app.schemas.article import ArticleRead
from backend.app.schemas.credibility import CredibilityAssessmentRead
from backend.app.schemas.publishing import PublishingRecordRead
from backend.app.schemas.research import ResearchBriefRead
from backend.app.schemas.scene import ScenePlanRead
from backend.app.schemas.script import NewsScriptRead
from backend.app.schemas.tts import NarrationAudioRead
from backend.app.schemas.video import VideoRenderRead


class QualityCheckRead(BaseModel):
    name: str
    status: str
    detail: str


class ApprovalRead(BaseModel):
    id: str
    video_render_id: str
    status: str
    reviewer: str | None
    notes: str | None
    quality_checks: list[dict[str, Any]]
    artifact_sha256: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReviewDecision(BaseModel):
    action: Literal["approve", "reject", "request_changes"]
    reviewer: str = Field(default="operator", min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=4_000)


class ReviewSummary(BaseModel):
    video_render_id: str
    script_id: str
    article_id: str
    title: str
    article_title: str
    source_name: str
    category: str | None
    country: str | None
    credibility_status: str | None
    credibility_score: float | None
    approval_status: str
    video_status: str
    duration_seconds: float
    created_at: datetime
    priority_score: float
    publish_status: str | None


class ReviewDetail(BaseModel):
    summary: ReviewSummary
    video: VideoRenderRead
    article: ArticleRead
    credibility: CredibilityAssessmentRead | None
    research: ResearchBriefRead | None
    script: NewsScriptRead
    scene_plan: ScenePlanRead
    narration: NarrationAudioRead | None
    approval: ApprovalRead
    quality_checks: list[dict[str, Any]]
    publishing_records: list[PublishingRecordRead]


class ScriptUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    hook: str | None = Field(default=None, min_length=1, max_length=2_000)
    introduction: str | None = Field(default=None, min_length=1, max_length=4_000)
    main_story: list[str] | None = None
    context: list[str] | None = None
    closing: str | None = Field(default=None, min_length=1, max_length=2_000)
    acknowledge_media_invalidation: bool = False
    actor: str = Field(default="operator", min_length=1, max_length=120)


class SceneUpdate(BaseModel):
    subtitle: str | None = Field(default=None, min_length=1, max_length=4_000)
    keywords: list[str] | None = None
    scene_description: str | None = Field(default=None, min_length=1, max_length=4_000)
    stock_footage_query: str | None = Field(default=None, min_length=1, max_length=1_000)
    image_search_query: str | None = Field(default=None, min_length=1, max_length=1_000)
    animation_suggestion: str | None = Field(default=None, min_length=1, max_length=1_000)
    transition: str | None = Field(default=None, min_length=1, max_length=80)
    acknowledge_media_invalidation: bool = False
    actor: str = Field(default="operator", min_length=1, max_length=120)


class ContentEditResult(BaseModel):
    message: str
    invalidated: bool
