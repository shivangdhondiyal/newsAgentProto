from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CredibilityEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    claim: str
    source_id: str
    publisher: str
    url: str
    title: str
    snippet: str | None
    relevance_score: float
    trust_weight: float
    finding: str


class CredibilityAssessmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    article_id: str
    status: str
    confidence_score: float
    explanation: str
    claims: list[str]
    independent_source_count: int
    extractor_name: str
    created_at: datetime
    updated_at: datetime
    evidence: list[CredibilityEvidenceRead]


class CredibilityRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, int | str]
