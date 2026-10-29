from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.app.core.config import Settings
from backend.app.media.audio import resolve_executable
from backend.app.media.audio_production import (
    AudioDelivery,
    build_delivery,
    list_voices,
    music_catalog,
    produce_audio,
    select_music,
    voice_delivery_for,
)
from backend.app.media.genres import build_effective_profile


def test_voice_catalog_reports_the_installed_local_voice() -> None:
    settings = Settings()
    voices = list_voices(settings)
    assert voices
    assert any(voice["provider"] == "piper" for voice in voices)
    assert all("available" in voice for voice in voices)


def test_genre_delivery_changes_pace_and_pauses() -> None:
    breaking = voice_delivery_for(build_effective_profile("breaking_news"))
    documentary = voice_delivery_for(build_effective_profile("documentary_deep_dive"))
    assert breaking.rate > documentary.rate
    assert breaking.sentence_pause_ms < documentary.sentence_pause_ms
    assert breaking.length_scale < documentary.length_scale


def test_sensitive_profiles_disable_music_even_when_requested() -> None:
    profile = build_effective_profile("crime_investigation")
    delivery = build_delivery(
        profile,
        overrides={"music_enabled": True, "music_level": 0.5},
        music_path=Path("assets/music/tense.mp3"),
    )
    assert delivery.music_enabled is False
    assert delivery.music_level == 0.0
    assert delivery.sfx_enabled is False
    assert "Sensitive" in delivery.music_reason


def test_music_selection_is_honest_when_the_library_is_empty(tmp_path: Path) -> None:
    settings = Settings(audio_music_dir=tmp_path / "music")
    profile = build_effective_profile("general_news")
    path, mood, reason = select_music(settings, profile)
    assert path is None
    assert mood
    assert "No music files" in reason
    assert music_catalog(settings) == []


def test_music_catalog_reads_operator_tracks(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "calm-editorial.mp3").write_bytes(b"not-real-audio")
    settings = Settings(audio_music_dir=root)
    catalog = music_catalog(settings)
    assert catalog[0]["name"] == "calm-editorial.mp3"
    assert "calm" in catalog[0]["moods"]
    assert catalog[0]["licensed_by_operator"] is True


@pytest.mark.asyncio
async def test_real_ffmpeg_post_production_and_validation(tmp_path: Path) -> None:
    ffmpeg = resolve_executable(Settings().video_ffmpeg_executable)
    if ffmpeg is None:
        pytest.skip("FFmpeg is not installed")
    source = tmp_path / "narration.wav"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=220:duration=3",
        "-af",
        "volume=0.5",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    assert source.is_file()

    profile = build_effective_profile("general_news")
    delivery: AudioDelivery = build_delivery(profile)
    result = await produce_audio(
        ffmpeg,
        source,
        tmp_path / "out",
        delivery=delivery,
        duration_seconds=3.0,
    )
    assert result.path.is_file()
    assert result.path.name == "final-mix.wav"
    assert result.validation.checks
    assert result.validation.integrated_lufs is not None
    assert result.validation.true_peak_dbtp is not None
    assert result.validation.true_peak_dbtp <= delivery.true_peak
    assert abs(result.validation.integrated_lufs - delivery.target_lufs) <= 1.0
    names = {check["name"] for check in result.to_public_dict()["checks"]}
    assert "narration_post" in names
    assert "music_subordination" in names
    assert "delivery_path" in names
    assert result.to_public_dict()["music_path"] is None
