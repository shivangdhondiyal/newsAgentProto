from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class VideoRenderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    narration_audio_id: str
    scene_plan_id: str
    status: str
    renderer_name: str
    video_path: str
    subtitle_path: str
    timeline_path: str
    duration_seconds: float
    width: int
    height: int
    fps: int
    created_at: datetime
    updated_at: datetime


class VideoRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, int | str]
