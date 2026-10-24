from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backend.app.db.models import Article, CredibilityAssessment, ResearchBrief


@dataclass(frozen=True)
class ScriptDraft:
    title: str
    hook: str
    introduction: str
    main_story: list[str]
    context: list[str]
    closing: str


class ScriptWriter(Protocol):
    name: str

    async def write(
        self,
        article: Article,
        assessment: CredibilityAssessment,
        research_brief: ResearchBrief,
    ) -> ScriptDraft: ...
