from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backend.app.db.models import Article


@dataclass(frozen=True)
class ResearchSourceCandidate:
    source_type: str
    source_id: str
    publisher: str
    url: str
    title: str
    snippet: str | None
    relevance_score: float


@dataclass(frozen=True)
class ResearchSummary:
    headline: str
    key_facts: list[str]
    context: list[str]
    uncertainties: list[str]


class ResearchProvider(Protocol):
    async def search(self, article: Article) -> list[ResearchSourceCandidate]: ...


class ResearchSummarizer(Protocol):
    name: str

    def summarize(
        self, article: Article, sources: list[ResearchSourceCandidate]
    ) -> ResearchSummary: ...
