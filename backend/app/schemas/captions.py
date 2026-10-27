from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.media.captions import (
    ALIGNMENTS,
    CAPTION_PRESETS,
    DEFAULT_CAPTION_PRESET,
    FONT_WEIGHTS,
    VERTICAL_POSITIONS,
    AspectLayout,
    CaptionSettings,
)


class AspectLayoutModel(BaseModel):
    font_size: int = Field(default=46, ge=20, le=120)
    max_chars_per_line: int = Field(default=42, ge=10, le=80)
    max_words_per_line: int = Field(default=9, ge=2, le=20)
    max_lines: int = Field(default=2, ge=1, le=4)
    vertical_position: Literal["bottom", "center", "top"] = "bottom"
    safe_bottom_ratio: float = Field(default=0.08, ge=0.0, le=0.40)
    safe_margin_ratio: float = Field(default=0.06, ge=0.0, le=0.30)
    line_spacing: float = Field(default=1.18, ge=0.9, le=2.0)

    def to_media(self) -> AspectLayout:
        return AspectLayout(**self.model_dump())


class CaptionSettingsModel(BaseModel):
    preset_key: str = Field(default=DEFAULT_CAPTION_PRESET, max_length=60)
    font_family: str = Field(default="Segoe UI", max_length=120)
    font_weight: Literal["regular", "medium", "semibold", "bold"] = "semibold"
    text_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    highlight_color: str = Field(default="#9DB2E6", pattern=r"^#[0-9A-Fa-f]{6}$")
    background_opacity: float = Field(default=0.45, ge=0.0, le=1.0)
    background_padding: int = Field(default=12, ge=0, le=48)
    outline_width: int = Field(default=2, ge=0, le=10)
    shadow: int = Field(default=1, ge=0, le=10)
    alignment: Literal["left", "center", "right"] = "center"
    timing_offset_ms: int = Field(default=0, ge=-5_000, le=5_000)
    speaker_labels: bool = False
    word_highlight: bool = False
    emphasis: bool = False
    uppercase: bool = False
    fade_ms: int = Field(default=0, ge=0, le=500)
    landscape: AspectLayoutModel = Field(
        default_factory=lambda: AspectLayoutModel(
            font_size=46,
            max_chars_per_line=42,
            max_words_per_line=9,
            max_lines=2,
            vertical_position="bottom",
            safe_bottom_ratio=0.08,
            safe_margin_ratio=0.06,
        )
    )
    portrait: AspectLayoutModel = Field(
        default_factory=lambda: AspectLayoutModel(
            font_size=58,
            max_chars_per_line=26,
            max_words_per_line=7,
            max_lines=3,
            vertical_position="bottom",
            safe_bottom_ratio=0.18,
            safe_margin_ratio=0.07,
        )
    )

    @field_validator("font_family")
    @classmethod
    def _font_family_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Choose a font family.")
        return value.strip()

    @model_validator(mode="after")
    def _validate_heuristics(self) -> CaptionSettingsModel:
        if self.background_opacity < 0.18 and self.outline_width < 1 and self.shadow < 1:
            # This is a readability warning rather than a hard rejection; the
            # service records it as a check so an operator can still choose it.
            return self
        return self

    def to_media(self) -> CaptionSettings:
        return CaptionSettings(
            preset_key=self.preset_key,
            font_family=self.font_family,
            font_weight=self.font_weight,
            text_color=self.text_color.upper(),
            highlight_color=self.highlight_color.upper(),
            background_opacity=self.background_opacity,
            background_padding=self.background_padding,
            outline_width=self.outline_width,
            shadow=self.shadow,
            alignment=self.alignment,
            timing_offset_ms=self.timing_offset_ms,
            speaker_labels=self.speaker_labels,
            word_highlight=self.word_highlight,
            emphasis=self.emphasis,
            uppercase=self.uppercase,
            fade_ms=self.fade_ms,
            landscape=self.landscape.to_media(),
            portrait=self.portrait.to_media(),
        )

    @classmethod
    def from_media(cls, value: CaptionSettings) -> CaptionSettingsModel:
        return cls.model_validate(value.to_dict())


class CaptionWordModel(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    emphasis: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> CaptionWordModel:
        if self.end < self.start:
            raise ValueError("Word end must not be before its start.")
        return self


class CaptionCueModel(BaseModel):
    index: int = Field(default=0, ge=0)
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    text: str = Field(min_length=1, max_length=1_000)
    words: list[CaptionWordModel] = []
    speaker: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _ordered(self) -> CaptionCueModel:
        if self.end <= self.start:
            raise ValueError("Caption end must be after its start.")
        if not self.text.strip():
            raise ValueError("Caption text cannot be empty.")
        return self


class CaptionTrackRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    script_id: str
    narration_audio_id: str | None = None
    status: str
    preset_key: str
    language: str
    source: str
    source_detail: str
    source_label: str
    settings: CaptionSettingsModel
    cues: list[CaptionCueModel]
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    edited: bool = False
    stale: bool = False
    stale_reason: str | None = None
    revision: int = 1
    audio_fingerprint: str = ""
    srt_url: str | None = None
    vtt_url: str | None = None
    ass_urls: dict[str, str] = {}
    duration_seconds: float | None = None
    created_at: datetime
    updated_at: datetime


class CaptionSettingsUpdate(BaseModel):
    preset_key: str | None = Field(default=None, max_length=60)
    settings: CaptionSettingsModel | None = None


class CaptionCuesUpdate(BaseModel):
    cues: list[CaptionCueModel]
    settings: CaptionSettingsModel | None = None


class CaptionRegenerateRequest(BaseModel):
    force: bool = False
    preset_key: str | None = Field(default=None, max_length=60)
    settings: CaptionSettingsModel | None = None


class CaptionImportRequest(BaseModel):
    format: Literal["srt", "vtt"] = "srt"
    content: str = Field(min_length=1, max_length=2_000_000)
    settings: CaptionSettingsModel | None = None


class CaptionPresetRead(BaseModel):
    key: str
    name: str
    description: str
    best_for: str
    settings: CaptionSettingsModel


class CaptionConfigRead(BaseModel):
    presets: list[CaptionPresetRead]
    defaults: dict[str, Any]
    fonts: list[dict[str, Any]]
    font_weights: list[str] = list(FONT_WEIGHTS)
    alignments: list[str] = list(ALIGNMENTS)
    vertical_positions: list[str] = list(VERTICAL_POSITIONS)
    statuses: list[str]
    source_labels: dict[str, str]
    transcription: dict[str, Any]
    burn_in_available: bool
    soft_subs_available: bool
    sidecar_available: bool


class CaptionJobRead(BaseModel):
    job_id: str
    track: CaptionTrackRead | None = None
    narration_audio_id: str | None = None
    duration_seconds: float | None = None
    can_generate: bool = False
    blocking_reason: str | None = None
    burn_in: bool = True
    soft_subs: bool = True
    sidecar: bool = True


def preset_payloads() -> list[dict[str, Any]]:
    return [
        {
            "key": preset.key,
            "name": preset.name,
            "description": preset.description,
            "best_for": preset.best_for,
            "settings": CaptionSettingsModel.from_media(preset.settings),
        }
        for preset in CAPTION_PRESETS.values()
    ]
