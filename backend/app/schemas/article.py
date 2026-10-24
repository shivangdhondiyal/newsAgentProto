from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class CollectedArticle(BaseModel):
    """Transport object produced by a collector before database ingestion."""

    source_id: str
    source_name: str
    url: HttpUrl
    canonical_url: str
    title: str = Field(min_length=1)
    summary: str | None = None
    raw_html: str | None = None
    normalized_text: str = Field(min_length=1)
    language: str | None = None
    category: str | None = None
    country: str | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArticleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_id: str
    source_name: str
    url: str
    canonical_url: str
    title: str
    summary: str | None
    normalized_text: str
    language: str | None
    category: str | None
    country: str | None
    published_at: datetime | None
    collected_at: datetime


class CollectionRunRead(BaseModel):
    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, Any]
