from __future__ import annotations

import re

from backend.app.agents.research.contracts import ResearchSourceCandidate, ResearchSummary
from backend.app.db.models import Article


class ExtractiveResearchSummarizer:
    """Builds a traceable briefing from source text without inventing or editorializing."""

    name = "extractive"

    def summarize(
        self, article: Article, sources: list[ResearchSourceCandidate]
    ) -> ResearchSummary:
        sentences = re.split(r"(?<=[.!?])\s+", article.normalized_text)
        key_facts = self._unique(
            [sentence.strip() for sentence in sentences if sentence.strip()], 4
        )
        context = self._unique(
            [
                source.snippet or source.title
                for source in sources
                if source.snippet or source.title
            ],
            4,
        )
        uncertainties = [
            "This briefing is extractive and does not resolve conflicting accounts.",
        ]
        if len({source.source_id for source in sources}) < 2:
            uncertainties.append(
                "Fewer than two additional independent context sources were retrieved."
            )
        return ResearchSummary(
            headline=article.title,
            key_facts=key_facts,
            context=context,
            uncertainties=uncertainties,
        )

    @staticmethod
    def _unique(items: list[str], limit: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            normalized = " ".join(item.split()).lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(" ".join(item.split()))
            if len(result) == limit:
                break
        return result
