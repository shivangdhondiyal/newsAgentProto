from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backend.app.db.models import NewsScript


@dataclass(frozen=True)
class PlannedScene:
    narration: str
    subtitle: str
    keywords: list[str]
    scene_description: str
    stock_footage_query: str
    image_search_query: str
    animation_suggestion: str
    transition: str


class ScenePlanner(Protocol):
    name: str

    def plan(self, script: NewsScript) -> list[PlannedScene]: ...
