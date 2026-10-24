from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backend.app.db.models import Article


@dataclass(frozen=True)
class EvidenceCandidate:
    source_id: str
    publisher: str
    url: str
    title: str
    snippet: str | None
    relevance_score: float


class ClaimExtractor(Protocol):
    name: str

    async def extract(self, article: Article) -> list[str]: ...


class EvidenceFinder(Protocol):
    async def find(self, claim: str, article: Article) -> list[EvidenceCandidate]: ...
