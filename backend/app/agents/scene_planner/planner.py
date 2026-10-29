from __future__ import annotations

from backend.app.agents.scene_planner.contracts import PlannedScene
from backend.app.db.models import NewsScript
from backend.app.media.keywords import search_keywords
from backend.app.media.script_text import strip_leading_headline, strip_meta_sentences


class RuleBasedScenePlanner:
    """Deterministic, editable planning without generating or licensing visual assets."""

    name = "rule_based"

    def plan(self, script: NewsScript) -> list[PlannedScene]:
        segments = self._segments(script)
        return [
            self._make_scene(segment, position, fallback_display=script.title)
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
        title = " ".join((script.title or "").split())
        # Production boilerplate ("Here is what is known so far...", "This
        # report is based on...") is spoken as well as shown, so it is removed
        # before narration is generated. A segment that is only boilerplate is
        # dropped instead of being narrated to the viewer.
        segments: list[str] = []
        for group in groups:
            if not group or not group.strip():
                continue
            cleaned = strip_meta_sentences(group)
            cleaned = strip_leading_headline(cleaned, title)
            if cleaned:
                segments.append(cleaned)
        if not segments and title:
            segments = [title]
        return segments

    def _make_scene(
        self, narration: str, position: int, *, fallback_display: str = ""
    ) -> PlannedScene:
        # Segments arrive already cleaned; keep the strip for callers that plan
        # raw script text directly. A segment that is *only* boilerplate falls
        # back to the story headline instead of a blank card.
        display = strip_meta_sentences(narration) or fallback_display or narration
        keywords = self._keywords(display)
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
            subtitle=display,
            keywords=keywords,
            scene_description=description,
            stock_footage_query=f"{query} neutral news footage",
            image_search_query=query,
            animation_suggestion=animation,
            transition="fade" if position == 1 else "crossfade",
        )

    @staticmethod
    def _keywords(text: str) -> list[str]:
        return search_keywords(text)
