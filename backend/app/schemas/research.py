from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ResearchSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_type: str
    source_id: str
    publisher: str
    url: str
    title: str
    snippet: str | None
    relevance_score: float


class ResearchBriefRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    article_id: str
    status: str
    headline: str
    key_facts: list[str]
    context: list[str]
    uncertainties: list[str]
    source_count: int
    summarizer_name: str
    created_at: datetime
    updated_at: datetime
    sources: list[ResearchSourceRead]


class ResearchRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, int | str]
