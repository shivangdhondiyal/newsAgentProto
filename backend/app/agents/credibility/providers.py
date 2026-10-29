from __future__ import annotations

import json
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.credibility.contracts import EvidenceCandidate
from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.agents.independence import (
    filter_independent_articles,
    filter_independent_publishers,
)
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


def _tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-zA-Z0-9]{3,}", value.lower()) if token not in STOP_WORDS
    }


def lexical_relevance(claim: str, candidate: str) -> float:
    claim_tokens = _tokens(claim)
    candidate_tokens = _tokens(candidate)
    if not claim_tokens or not candidate_tokens:
        return 0.0
    return len(claim_tokens & candidate_tokens) / len(claim_tokens)


class HeuristicClaimExtractor:
    """Safe fallback: returns reported statements but never presents them as verified facts."""

    name = "heuristic"

    async def extract(self, article: Article) -> list[str]:
        sentences = re.split(r"(?<=[.!?])\s+", article.normalized_text)
        candidates = [article.title, *sentences[:2]]
        claims: list[str] = []
        for candidate in candidates:
            cleaned = " ".join(candidate.split())
            if cleaned and cleaned not in claims:
                claims.append(cleaned)
        return claims[:3]


class OllamaClaimExtractor:
    """Local structured-output extractor; it never sends article text to a paid API."""

    name = "ollama"

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def extract(self, article: Article) -> list[str]:
        prompt = (
            "Extract up to three atomic, factual claims from this news report. "
            'Do not add facts or opinions. Return JSON only: {"claims": [string]}.\n\n'
            f"Title: {article.title}\nBody: {article.normalized_text[:12_000]}"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "format": "json",
                },
            )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
        payload = json.loads(content)
        claims = payload.get("claims", [])
        if not isinstance(claims, list) or not all(isinstance(claim, str) for claim in claims):
            raise ValueError("Ollama did not return a string claims list")
        return [" ".join(claim.split()) for claim in claims if claim.strip()][:3]


class LocalEvidenceFinder:
    """Find candidate corroboration in the stored corpus from independent publishers."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], trust_policy: SourceTrustPolicy
    ) -> None:
        self._session_factory = session_factory
        self._trust_policy = trust_policy

    async def find(self, claim: str, article: Article) -> list[EvidenceCandidate]:
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
                        lexical_relevance(claim, f"{candidate.title} {candidate.normalized_text}"),
                    )
                    for candidate in candidates
                ),
                key=lambda item: item[1],
                reverse=True,
            )
            relevant = [candidate for candidate, relevance in scored if relevance >= 0.35]
            independent = await filter_independent_articles(session, article.id, relevant)
            independent = filter_independent_publishers(article, independent, self._trust_policy)
            relevance_by_id = {candidate.id: relevance for candidate, relevance in scored}

        return [
            EvidenceCandidate(
                source_id=candidate.source_id,
                publisher=candidate.source_name,
                url=candidate.url,
                title=candidate.title,
                snippet=candidate.summary,
                relevance_score=relevance_by_id[candidate.id],
            )
            for candidate in independent
        ]
