from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Environment variables use the NEWS_ prefix."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="NEWS_", extra="ignore")

    app_name: str = "News Automation Platform"
    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./data/news_automation.db"
    sources_file: Path = Path("config/sources.yml")
    collection_enabled: bool = True
    collection_interval_minutes: int = Field(default=15, ge=1, le=1440)
    deduplication_enabled: bool = True
    deduplication_interval_minutes: int = Field(default=15, ge=1, le=1440)
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    deduplication_auto_threshold: float = Field(default=0.88, ge=0.0, le=1.0)
    deduplication_review_threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    credibility_enabled: bool = True
    credibility_interval_minutes: int = Field(default=20, ge=1, le=1440)
    credibility_batch_size: int = Field(default=10, ge=1, le=100)
    credibility_confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    trust_policy_file: Path = Path("config/trust.yml")
    claim_extraction_provider: str = "heuristic"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    research_enabled: bool = True
    research_interval_minutes: int = Field(default=25, ge=1, le=1440)
    research_batch_size: int = Field(default=10, ge=1, le=100)
    research_external_provider: str = "none"
    research_max_sources: int = Field(default=8, ge=1, le=25)
    script_writer_enabled: bool = True
    script_writer_interval_minutes: int = Field(default=30, ge=1, le=1440)
    script_writer_batch_size: int = Field(default=10, ge=1, le=100)
    script_writer_provider: str = "extractive"
    script_target_seconds: int = Field(default=60, ge=15, le=600)
    script_minimum_research_sources: int = Field(default=1, ge=0, le=10)
    scene_planner_enabled: bool = True
    scene_planner_interval_minutes: int = Field(default=30, ge=1, le=1440)
    scene_planner_batch_size: int = Field(default=10, ge=1, le=100)
    scene_min_duration_seconds: int = Field(default=3, ge=1, le=20)
    tts_enabled: bool = True
    tts_interval_minutes: int = Field(default=30, ge=1, le=1440)
    tts_batch_size: int = Field(default=5, ge=1, le=50)
    tts_provider: str = "piper"
    tts_piper_executable: str = "piper"
    tts_piper_voice_model: Path = Path("models/piper/en_US-lessac-medium.onnx")
    tts_chunk_max_characters: int = Field(default=450, ge=80, le=2_000)
    tts_output_dir: Path = Path("assets/generated/audio")
    tts_generate_mp3: bool = False
    tts_ffmpeg_executable: str = "ffmpeg"
    tts_length_scale: float = Field(default=1.04, ge=0.7, le=1.5)
    tts_noise_scale: float = Field(default=0.667, ge=0.0, le=1.5)
    tts_noise_w: float = Field(default=0.8, ge=0.0, le=1.5)
    tts_sentence_silence: float = Field(default=0.22, ge=0.0, le=2.0)
    audio_master_enabled: bool = True
    audio_target_lufs: float = Field(default=-16.0, ge=-30.0, le=-8.0)
    audio_true_peak_db: float = Field(default=-1.5, ge=-6.0, le=0.0)
    audio_sample_rate: int = Field(default=48_000, ge=8_000, le=192_000)
    audio_channels: int = Field(default=2, ge=1, le=2)
    audio_sentence_pause_ms: int = Field(default=180, ge=0, le=2_000)
    audio_paragraph_pause_ms: int = Field(default=420, ge=0, le=3_000)
    audio_music_enabled: bool = False
    audio_music_dir: Path = Path("assets/music")
    audio_music_ducking: bool = True
    audio_output_dir: Path = Path("assets/generated/audio/mastered")
    asset_enabled: bool = True
    asset_provider_priority: str = "wikimedia,nasa"
    asset_strategy_default: str = "hybrid"
    asset_cache_dir: Path = Path("assets/cache/media")
    asset_max_bytes: int = Field(default=20_000_000, ge=100_000, le=200_000_000)
    asset_min_width: int = Field(default=640, ge=100, le=8_000)
    asset_min_height: int = Field(default=360, ge=100, le=8_000)
    asset_requests_per_scene: int = Field(default=3, ge=1, le=10)
    # Licensed but off-subject imagery is worse than an honest designed graphic.
    asset_min_relevance: float = Field(default=0.35, ge=0.0, le=1.0)
    asset_download_timeout_seconds: float = Field(default=25.0, gt=0, le=120)
    # Wikimedia Commons rejects user agents without a contact URL (HTTP 403).
    asset_user_agent: str = (
        "NewsAutomationPlatform/0.2 (self-hosted newsroom; +https://localhost/)"
    )
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    video_enabled: bool = True
    video_interval_minutes: int = Field(default=30, ge=1, le=1440)
    video_batch_size: int = Field(default=3, ge=1, le=25)
    video_ffmpeg_executable: str = "ffmpeg"
    video_output_dir: Path = Path("assets/generated/video")
    video_width: int = Field(default=1920, ge=320, le=3840)
    video_height: int = Field(default=1080, ge=240, le=2160)
    video_fps: int = Field(default=30, ge=1, le=60)
    video_render_timeout_seconds: float = Field(default=900.0, ge=30, le=7_200)
    # Encoding happens here first, then the finished file is moved into place.
    # Keeping the scratch area out of a cloud-synced media folder avoids the
    # truncated-file hang caused by sync clients and scanners holding the output.
    video_scratch_dir: Path = Path(tempfile.gettempdir()) / "news-render"
    video_background_color: str = "0x0F172A"
    video_default_style: str = "broadcast"
    video_aspects: str = "16:9,9:16"
    video_landscape_width: int = Field(default=1920, ge=320, le=3840)
    video_landscape_height: int = Field(default=1080, ge=240, le=2160)
    video_portrait_width: int = Field(default=1080, ge=240, le=2160)
    video_portrait_height: int = Field(default=1920, ge=320, le=3840)
    video_captions_enabled: bool = True
    video_transition_seconds: float = Field(default=0.4, ge=0.0, le=2.0)
    video_frame_cache_dir: Path = Path("assets/generated/frames")
    caption_enabled: bool = True
    caption_output_dir: Path = Path("assets/generated/captions")
    caption_default_preset: str = "clean_editorial"
    caption_transcription_provider: str = "auto"
    caption_whisper_model: str = "base"
    caption_language: str = "auto"
    caption_fonts_dir: Path | None = None
    caption_burn_in_default: bool = True
    caption_soft_subs_default: bool = True
    caption_sidecar_default: bool = True
    production_enabled: bool = True
    production_max_retries: int = Field(default=1, ge=0, le=5)
    production_stage_timeout_seconds: int = Field(default=1_800, ge=30, le=14_400)
    production_defaults_file: Path = Path("data/production_defaults.json")
    approval_min_video_bytes: int = Field(default=1024, ge=1, le=100_000_000)
    publishing_enabled: bool = True
    publishing_interval_minutes: int = Field(default=60, ge=1, le=1440)
    publishing_batch_size: int = Field(default=5, ge=1, le=25)
    publishing_default_platform: str = "local"
    publishing_output_dir: Path = Path("assets/published")
    publishing_youtube_client_id: str = ""
    publishing_youtube_client_secret: str = ""
    publishing_youtube_refresh_token: str = ""
    publishing_youtube_privacy_status: str = "private"
    publishing_youtube_category_id: str = "25"
    publishing_youtube_default_language: str = "en"
    publishing_youtube_made_for_kids: bool = False
    request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    max_articles_per_source: int = Field(default=20, ge=1, le=100)
    user_agent: str = "NewsAutomationPlatform/0.1"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
