from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SceneRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int
    start_seconds: int
    duration_seconds: int
    narration: str
    subtitle: str
    keywords: list[str]
    scene_description: str
    stock_footage_query: str
    image_search_query: str
    animation_suggestion: str
    transition: str


class ScenePlanRead(BaseModel):
    id: str
    script_id: str
    status: str
    total_duration_seconds: int
    planner_name: str
    created_at: datetime
    updated_at: datetime
    scenes: list[SceneRead]


class ScenePlanRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, int | str]
