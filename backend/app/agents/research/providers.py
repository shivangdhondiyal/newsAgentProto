from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.agents.independence import (
    filter_independent_articles,
    filter_independent_publishers,
)
from backend.app.agents.research.contracts import ResearchProvider, ResearchSourceCandidate
from backend.app.db.models import Article

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "was",
    "were",
    "with",
}


def lexical_relevance(query: str, candidate: str) -> float:
    query_terms = {
        word for word in re.findall(r"[a-zA-Z0-9]{3,}", query.lower()) if word not in STOP_WORDS
    }
    candidate_terms = {
        word for word in re.findall(r"[a-zA-Z0-9]{3,}", candidate.lower()) if word not in STOP_WORDS
    }
    if not query_terms or not candidate_terms:
        return 0.0
    return len(query_terms & candidate_terms) / len(query_terms)


def trim_snippet(value: str, limit: int = 400) -> str:
    """Keep research context short and prefer a sentence boundary."""

    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    candidate = text[:limit]
    boundary = max(candidate.rfind(". "), candidate.rfind("! "), candidate.rfind("? "))
    if boundary >= limit // 2:
        return candidate[: boundary + 1]
    return candidate.rstrip() + "…"


class LocalCorpusResearchProvider:
    """Independent-source context retrieval with no network or paid dependency."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        max_sources: int,
        trust_policy: SourceTrustPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._max_sources = max_sources
        self._trust_policy = trust_policy

    async def search(self, article: Article) -> list[ResearchSourceCandidate]:
        query = f"{article.title} {article.normalized_text[:2_000]}"
        async with self._session_factory() as session:
            statement = (
                select(Article)
                .where(Article.id != article.id)
                .order_by(Article.published_at.desc())
                .limit(250)
            )
            candidates = list((await session.scalars(statement)).all())
            scored = sorted(
                (
                    (
                        candidate,
                        lexical_relevance(query, f"{candidate.title} {candidate.normalized_text}"),
                    )
                    for candidate in candidates
                ),
                key=lambda item: item[1],
                reverse=True,
            )
            relevant = [candidate for candidate, relevance in scored if relevance >= 0.30]
            independent = await filter_independent_articles(session, article.id, relevant)
            independent = filter_independent_publishers(article, independent, self._trust_policy)
            relevance_by_id = {candidate.id: relevance for candidate, relevance in scored}

        findings = [
            ResearchSourceCandidate(
                source_type="local_corpus",
                source_id=candidate.source_id,
                publisher=candidate.source_name,
                url=candidate.url,
                title=candidate.title,
                snippet=trim_snippet(candidate.summary or candidate.normalized_text),
                relevance_score=relevance_by_id[candidate.id],
            )
            for candidate in independent
        ]
        return findings[: self._max_sources]


class GdeltResearchProvider:
    """Optional public, key-free adapter filtered through the configured trust policy."""

    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(self, trust_policy: SourceTrustPolicy, max_sources: int) -> None:
        self._trust_policy = trust_policy
        self._max_sources = max_sources

    async def search(self, article: Article) -> list[ResearchSourceCandidate]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                self.endpoint,
                params={
                    "query": f'"{article.title}"',
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": self._max_sources * 3,
                    "timespan": "1week",
                },
            )
        response.raise_for_status()
        records = response.json().get("articles", [])
        findings: list[ResearchSourceCandidate] = []
        for record in records:
            url = record.get("url")
            title = record.get("title")
            if not isinstance(url, str) or not isinstance(title, str):
                continue
            trust_weight = self._trust_policy.weight_for_url(url)
            if trust_weight <= 0.35:
                continue
            domain = urlparse(url).hostname or "unknown-source"
            findings.append(
                ResearchSourceCandidate(
                    source_type="gdelt",
                    source_id=f"domain:{domain}",
                    publisher=domain,
                    url=url,
                    title=title,
                    snippet=None,
                    relevance_score=lexical_relevance(article.title, title),
                )
            )
        return findings[: self._max_sources]


class CompositeResearchProvider:
    def __init__(self, providers: list[ResearchProvider]) -> None:
        self._providers = providers

    async def search(self, article: Article) -> list[ResearchSourceCandidate]:
        results: list[ResearchSourceCandidate] = []
        seen_urls: set[str] = set()
        for provider in self._providers:
            for source in await provider.search(article):
                if source.url not in seen_urls:
                    seen_urls.add(source.url)
                    results.append(source)
        return results
