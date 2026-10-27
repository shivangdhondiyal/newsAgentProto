from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AspectChoice = Literal["both", "16:9", "9:16"]


class ProductionJobCreate(BaseModel):
    script_id: str | None = None
    article_id: str | None = None
    style_key: str = Field(default="broadcast", max_length=60)
    aspect_ratio: AspectChoice = "both"
    captions: bool = True
    asset_strategy: str = Field(default="hybrid", max_length=40)
    voice: str | None = Field(default=None, max_length=255)
    destination: str = Field(default="local", max_length=40)
    music_path: str | None = Field(default=None, max_length=1024)
    regenerate_narration: bool = False
    requested_by: str = Field(default="operator", max_length=120)


class ProductionStageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    label: str
    status: str
    attempts: int
    error: str | None = None
    logs: list[dict[str, Any]] = []
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None


class VideoVariantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    aspect_ratio: str
    style_key: str
    width: int
    height: int
    fps: int
    video_path: str
    subtitle_path: str
    timeline_path: str
    duration_seconds: float
    status: str
    checks_json: list[dict[str, Any]] = []
    file_url: str | None = None


class AssetLicenseRead(BaseModel):
    scene_position: int
    scene_id: str
    status: str
    match_reason: str
    title: str | None = None
    provider: str | None = None
    creator: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    source_url: str | None = None
    local_path: str | None = None


class ScenePreviewRead(BaseModel):
    position: int
    start_seconds: float
    duration_seconds: float
    subtitle: str
    narration: str
    transition: str
    frame_16x9: str | None = None
    frame_9x16: str | None = None
    asset: AssetLicenseRead | None = None


class ProductionJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    current_stage: str | None = None
    script_id: str | None = None
    article_id: str | None = None
    video_render_id: str | None = None
    style_key: str
    aspect_ratio: str
    requested_by: str
    fingerprint: str
    config_json: dict[str, Any] = {}
    warnings: list[str] = []
    error: str | None = None
    logs: list[dict[str, Any]] = []
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    stages: list[ProductionStageRead] = []
    variants: list[VideoVariantRead] = []
    scenes: list[ScenePreviewRead] = []
    title: str | None = None
    article_title: str | None = None
    script_title: str | None = None
    approval_status: str | None = None
    review_url: str | None = None


class ProductionStyleRead(BaseModel):
    key: str
    name: str
    description: str
    best_for: str
    transition: str
    motion: str
    caption_style: str
    graphics: list[str]
    asset_priority: list[str]
    music: str
    craft: dict[str, Any]
    landscape: dict[str, Any]
    portrait: dict[str, Any]


class ProductionStoryRead(BaseModel):
    script_id: str
    article_id: str
    title: str
    article_title: str
    source_name: str
    status: str
    review_reasons: list[str] = []
    credibility_score: float | None = None
    credibility_status: str | None = None
    estimated_duration_seconds: int
    has_scene_plan: bool
    has_narration: bool
    has_render: bool
    asset_ready: int = 0
    asset_fallback: int = 0
    production_ready: bool = True
    blocking_reasons: list[str] = []
    created_at: datetime


class ProductionConfigRead(BaseModel):
    defaults: dict[str, Any]
    styles: list[ProductionStyleRead]
    asset_providers: list[str]
    asset_strategy: str
    voice_name: str
    ffmpeg_available: bool
    piper_available: bool
    captions_available: bool
    music_dir: str
    destination_options: list[str]
    aspect_options: list[str]
    duration_limits: dict[str, int]
    licensing_note: str


class ProductionDefaultsUpdate(BaseModel):
    style_key: str | None = None
    aspect_ratio: AspectChoice | None = None
    captions: bool | None = None
    asset_strategy: str | None = None
    voice: str | None = None
    destination: str | None = None
