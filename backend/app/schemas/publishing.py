from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublishingQueueRequest(BaseModel):
    video_render_id: str = Field(min_length=1, max_length=36)
    platform: str | None = Field(default=None, min_length=1, max_length=50)
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=4_900)
    tags: list[str] = Field(default_factory=list, max_length=30)


class PublishingRetryRequest(BaseModel):
    platform: str | None = Field(default=None, max_length=50)


class PublishingRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_render_id: str
    approval_id: str
    platform: str
    status: str
    title: str
    description: str
    tags: list[str]
    external_id: str | None
    external_url: str | None
    idempotency_key: str
    attempts: int
    last_error: str | None
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


class PublishingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, int | str]


class PublishingAnalyticsRead(BaseModel):
    days: int
    by_status: dict[str, int]
    by_platform: dict[str, int]
    approved_videos: int
    pending_reviews: int
    publishing_success_rate: float
    external_publish_count: int
    recent_records: list[PublishingRecordRead]


class ReviewAnalyticsRead(BaseModel):
    pending: int
    approved: int
    rejected: int
    changes_requested: int
    published: int
    exported: int
