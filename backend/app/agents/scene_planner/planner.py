from __future__ import annotations

import re

from backend.app.agents.scene_planner.contracts import PlannedScene
from backend.app.db.models import NewsScript

STOP_WORDS = {
    "about",
    "after",
    "against",
    "also",
    "and",
    "are",
    "been",
    "being",
    "by",
    "for",
    "from",
    "has",
    "have",
    "here",
    "into",
    "its",
    "known",
    "more",
    "report",
    "said",
    "that",
    "the",
    "their",
    "this",
    "time",
    "was",
    "were",
    "what",
    "with",
}


class RuleBasedScenePlanner:
    """Deterministic, editable planning without generating or licensing visual assets."""

    name = "rule_based"

    def plan(self, script: NewsScript) -> list[PlannedScene]:
        segments = self._segments(script)
        return [
            self._make_scene(segment, position)
            for position, segment in enumerate(segments, start=1)
        ]

    def _segments(self, script: NewsScript) -> list[str]:
        groups = [
            script.hook,
            script.introduction,
            *script.main_story,
            *script.context,
            script.closing,
        ]
        return [" ".join(group.split()) for group in groups if group and group.strip()]

    def _make_scene(self, narration: str, position: int) -> PlannedScene:
        keywords = self._keywords(narration)
        query = " ".join(keywords) or "news context"
        if position == 1:
            description = "Use a restrained opening title card or relevant establishing image."
            animation = "Slow fade in with a subtle title reveal."
        elif position % 2 == 0:
            description = (
                "Use relevant factual footage, a location view, or a neutral data graphic."
            )
            animation = "Gentle pan or slow zoom; avoid dramatic motion."
        else:
            description = (
                "Use a contextual still, map, document, or clearly labeled archival visual."
            )
            animation = "Simple crossfade with a subtle caption reveal."
        return PlannedScene(
            narration=narration,
            subtitle=narration,
            keywords=keywords,
            scene_description=description,
            stock_footage_query=f"{query} neutral news footage",
            image_search_query=f"{query} editorial image",
            animation_suggestion=animation,
            transition="fade" if position == 1 else "crossfade",
        )

    @staticmethod
    def _keywords(text: str) -> list[str]:
        candidates = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}", text.lower())
        unique: list[str] = []
        for candidate in candidates:
            if candidate not in STOP_WORDS and candidate not in unique:
                unique.append(candidate)
            if len(unique) == 5:
                break
        return unique
