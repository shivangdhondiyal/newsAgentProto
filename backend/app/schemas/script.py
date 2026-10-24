from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NewsScriptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    article_id: str
    research_brief_id: str
    status: str
    title: str
    hook: str
    introduction: str
    main_story: list[str]
    context: list[str]
    closing: str
    full_text: str
    estimated_duration_seconds: int
    target_duration_seconds: int
    writer_name: str
    review_reasons: list[str]
    created_at: datetime
    updated_at: datetime


class ScriptRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, int | str]
