from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DeduplicationRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, int | str]


class DuplicateClusterMemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    article_id: str
    similarity: float
    role: str


class DuplicateClusterRead(BaseModel):
    id: str
    canonical_article_id: str
    status: str
    created_at: datetime
    resolved_at: datetime | None
    members: list[DuplicateClusterMemberRead]


class DuplicateClusterResolution(BaseModel):
    action: Literal["confirm", "reject"]


class DuplicateClusterQuery(BaseModel):
    status: Literal["review", "confirmed", "rejected"] | None = None
    limit: int = Field(default=50, ge=1, le=100)
