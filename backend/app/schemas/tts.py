from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NarrationChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    position: int
    text: str
    wav_path: str
    start_seconds: float
    duration_seconds: float


class NarrationAudioRead(BaseModel):
    id: str
    script_id: str
    scene_plan_id: str
    status: str
    provider_name: str
    voice_name: str
    wav_path: str
    mp3_path: str | None
    duration_seconds: float
    sample_rate: int
    created_at: datetime
    updated_at: datetime
    chunks: list[NarrationChunkRead]


class TTSRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, int | str]
