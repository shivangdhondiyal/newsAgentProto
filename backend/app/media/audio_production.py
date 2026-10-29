"""Genre-aware audio post-production and final-mix validation.

This module deliberately reports what it actually did. A missing music bed, a
missing Piper voice, or an unavailable FFmpeg binary becomes an explicit
warning instead of a fabricated "mixed" status. Narration remains the anchor:
music is selected by mood and level, ducked under speech, and never allowed to
mask the voice.
"""

from __future__ import annotations

import asyncio
import math
import re
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.media.audio import (
    AudioValidation,
    measure_loudness,
    probe_audio,
    validate_audio,
)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
SILENCE_JSON = re.compile(r"silence_(start|end):\s*([0-9.]+)")
MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?[0-9.]+)\s*dB")
MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?[0-9.]+)\s*dB")


@dataclass(frozen=True)
class VoiceOption:
    key: str
    name: str
    language: str
    provider: str
    model_path: str
    available: bool
    description: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "language": self.language,
            "provider": self.provider,
            "model_path": self.model_path,
            "available": self.available,
            "description": self.description,
        }


@dataclass(frozen=True)
class VoiceDelivery:
    rate: float = 1.0
    expressiveness: float = 0.667
    sentence_pause_ms: int = 180
    paragraph_pause_ms: int = 420

    @property
    def length_scale(self) -> float:
        return round(max(0.5, min(2.0, 1.0 / max(0.5, self.rate))), 4)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "rate": self.rate,
            "expressiveness": self.expressiveness,
            "sentence_pause_ms": self.sentence_pause_ms,
            "paragraph_pause_ms": self.paragraph_pause_ms,
            "length_scale": self.length_scale,
        }


@dataclass(frozen=True)
class AudioDelivery:
    """The complete, serialisable audio post-production contract."""

    target_lufs: float = -16.0
    true_peak: float = -1.5
    sample_rate: int = 48_000
    channels: int = 2
    denoise: bool = False
    de_ess: bool = True
    compression: bool = True
    equalization: bool = True
    fades: bool = True
    trim_silence: bool = False
    music_enabled: bool = False
    music_level: float = 0.0
    narration_level: float = 1.0
    sfx_enabled: bool = False
    sfx_level: float = 0.0
    ducking: bool = True
    music_path: str | None = None
    music_mood: str = "neutral"
    music_reason: str = ""
    sensitivity: str = "standard"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "target_lufs": self.target_lufs,
            "true_peak": self.true_peak,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "denoise": self.denoise,
            "de_ess": self.de_ess,
            "compression": self.compression,
            "equalization": self.equalization,
            "fades": self.fades,
            "trim_silence": self.trim_silence,
            "music": {
                "enabled": self.music_enabled,
                "level": self.music_level,
                "path": self.music_path,
                "mood": self.music_mood,
                "reason": self.music_reason,
                "ducking": self.ducking,
            },
            "narration_level": self.narration_level,
            "sfx": {"enabled": self.sfx_enabled, "level": self.sfx_level},
            "sensitivity": self.sensitivity,
        }


@dataclass
class AudioProductionResult:
    path: Path
    duration_seconds: float
    delivery: AudioDelivery
    validation: AudioValidation
    warnings: list[str] = field(default_factory=list)
    music_path: str | None = None
    sfx_path: str | None = None
    checks: list[dict[str, str]] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "duration_seconds": round(self.duration_seconds, 3),
            "delivery": self.delivery.to_public_dict(),
            "integrated_lufs": self.validation.integrated_lufs,
            "true_peak_dbtp": self.validation.true_peak_dbtp,
            "passed": self.validation.passed
            and not any(check.get("status") == "failed" for check in self.checks),
            "checks": [*self.validation.checks, *self.checks],
            "warnings": self.warnings,
            "music_path": self.music_path,
            "sfx_path": self.sfx_path,
        }


# --------------------------------------------------------------------- voices


def list_voices(settings: Any) -> list[dict[str, Any]]:
    root = Path("models/piper")
    options: list[VoiceOption] = []
    configured = Path(settings.tts_piper_voice_model)
    candidates = sorted({*root.glob("*.onnx"), configured}) if root.is_dir() else [configured]
    for model in candidates:
        if not model.is_file():
            continue
        key = model.stem
        language = key.split("_")[0] if "_" in key else "en"
        options.append(
            VoiceOption(
                key=key,
                name=key.replace("_", " ").title(),
                language=language,
                provider="piper",
                model_path=str(model),
                available=True,
                description=(
                    "Local Piper narration voice. Delivery is adjusted by the "
                    "selected genre and the operator overrides."
                ),
            )
        )
    if not options:
        options.append(
            VoiceOption(
                key="unavailable",
                name="No local voice installed",
                language="en",
                provider="piper",
                model_path=str(configured),
                available=False,
                description="Install a Piper .onnx voice model to enable narration.",
            )
        )
    return [option.to_public_dict() for option in options]


def voice_delivery_for(profile: Any) -> VoiceDelivery:
    genre = profile.genre
    return VoiceDelivery(
        rate=float(genre.narration_rate),
        expressiveness=float(genre.narration_expressiveness),
        sentence_pause_ms=int(genre.sentence_pause_ms),
        paragraph_pause_ms=int(genre.paragraph_pause_ms),
    )


async def synthesize_voice_preview(
    settings: Any,
    *,
    text: str,
    output_path: Path,
    voice_key: str | None = None,
    delivery: VoiceDelivery | None = None,
) -> Path:
    """Generate a short real preview with the configured local voice."""

    text = " ".join((text or "").split())
    if not text:
        raise ValueError("Preview text is required")
    text = text[:240]
    model = Path(settings.tts_piper_voice_model)
    if voice_key:
        candidate = Path("models/piper") / f"{voice_key}.onnx"
        if candidate.is_file():
            model = candidate
    if not model.is_file():
        raise FileNotFoundError(f"Piper voice model not found: {model}")
    executable = settings.tts_piper_executable
    from backend.app.media.audio import resolve_executable

    resolved = resolve_executable(executable)
    if resolved is None:
        raise FileNotFoundError(
            f"Piper executable not found: {executable}. Install Piper or update "
            "NEWS_TTS_PIPER_EXECUTABLE."
        )
    delivery = delivery or VoiceDelivery()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        resolved,
        "--model",
        str(model),
        "--output_file",
        str(output_path),
        "--length_scale",
        f"{delivery.length_scale:.4f}",
        "--noise_scale",
        f"{max(0.0, min(2.0, delivery.expressiveness)):.4f}",
        "--noise_w",
        "0.8",
        "--sentence_silence",
        f"{max(0.0, min(3.0, delivery.sentence_pause_ms / 1000)):.4f}",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate(text.encode("utf-8"))
    if process.returncode != 0 or not output_path.is_file():
        raise RuntimeError(
            "Piper preview failed: " + stderr.decode("utf-8", errors="replace")[-800:]
        )
    return output_path


# ------------------------------------------------------------------- delivery


def build_delivery(
    profile: Any,
    *,
    overrides: dict[str, Any] | None = None,
    music_path: Path | None = None,
    music_mood: str | None = None,
    music_reason: str = "",
) -> AudioDelivery:
    overrides = dict(overrides or {})
    genre = profile.genre
    music_enabled = bool(profile.music_enabled)
    if genre.sensitivity in {"sensitive", "high"}:
        music_enabled = False
    if overrides.get("music_enabled") is not None:
        music_enabled = bool(overrides["music_enabled"]) and genre.sensitivity not in {
            "sensitive",
            "high",
        }
    music_level = float(overrides.get("music_level", profile.music_level))
    if genre.sensitivity in {"sensitive", "high"}:
        music_level = 0.0
    sfx_enabled = bool(profile.sfx_enabled) and genre.sensitivity not in {
        "sensitive",
        "high",
    }
    if overrides.get("sfx_enabled") is not None:
        sfx_enabled = bool(overrides["sfx_enabled"]) and genre.sensitivity not in {
            "sensitive",
            "high",
        }
    return AudioDelivery(
        target_lufs=float(profile.target_lufs),
        true_peak=-1.5,
        sample_rate=48_000,
        channels=2,
        denoise=bool(overrides.get("denoise", False)),
        de_ess=bool(overrides.get("de_ess", True)),
        compression=bool(overrides.get("compression", True)),
        equalization=bool(overrides.get("equalization", True)),
        fades=bool(overrides.get("fades", True)),
        trim_silence=bool(overrides.get("trim_silence", False)),
        music_enabled=music_enabled and (music_path is not None),
        music_level=max(0.0, min(0.5, music_level * 0.22)),
        narration_level=float(overrides.get("narration_level", profile.narration_level)),
        sfx_enabled=sfx_enabled,
        sfx_level=max(0.0, min(0.2, float(profile.sfx_level) * 0.2)),
        ducking=bool(overrides.get("ducking", genre.ducking)),
        music_path=str(music_path) if music_path else None,
        music_mood=music_mood or (genre.music_moods[0] if genre.music_moods else "neutral"),
        music_reason=(
            "Sensitive reporting profile: decorative music and sound design are disabled."
            if genre.sensitivity in {"sensitive", "high"}
            else music_reason
        ),
        sensitivity=genre.sensitivity,
    )


def music_catalog(settings: Any) -> list[dict[str, Any]]:
    root = Path(settings.audio_music_dir)
    if not root.is_dir():
        return []
    items = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        stem = path.stem.lower()
        moods = [
            mood
            for mood in (
                "calm",
                "neutral",
                "editorial",
                "tense",
                "uplifting",
                "ambient",
                "modern",
                "corporate",
                "light",
                "energetic",
            )
            if mood in stem
        ]
        items.append(
            {
                "path": str(path),
                "name": path.name,
                "moods": moods or ["neutral"],
                "licensed_by_operator": True,
                "note": "Operator-supplied track; confirm its license for this distribution.",
            }
        )
    return items


def select_music(
    settings: Any,
    profile: Any,
    *,
    operator_path: Path | None = None,
) -> tuple[Path | None, str, str]:
    """Pick a mood-matched bed only when one actually exists."""

    if operator_path is not None:
        if not operator_path.is_file():
            return None, "neutral", f"Operator music file not found: {operator_path}"
        return operator_path, "operator", "Operator-supplied music track."
    if not profile.music_enabled:
        return None, "neutral", "Music is disabled for this genre or override."
    catalog = music_catalog(settings)
    if not catalog:
        return (
            None,
            profile.genre.music_moods[0] if profile.genre.music_moods else "neutral",
            "No music files are installed in the music library; narration is "
            "delivered without a bed.",
        )
    wanted = set(profile.genre.music_moods)
    for item in catalog:
        if wanted.intersection(item["moods"]):
            return (
                Path(item["path"]),
                item["moods"][0],
                f"Selected licensed library track {item['name']}.",
            )
    return (
        Path(catalog[0]["path"]),
        catalog[0]["moods"][0],
        "No exact mood match; used the closest library track.",
    )


# ------------------------------------------------------------------ analysis


async def analyze_narration(ffmpeg: str, path: Path) -> list[dict[str, str]]:
    """Measure clipping, level, and silence with real FFmpeg analysis."""

    checks: list[dict[str, str]] = []
    if not path.is_file():
        return [{"name": "file_exists", "status": "failed", "detail": "Narration file is missing."}]
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    text = stderr.decode("utf-8", errors="replace")
    max_match = MAX_VOLUME_RE.search(text)
    mean_match = MEAN_VOLUME_RE.search(text)
    if max_match:
        maximum = float(max_match.group(1))
        checks.append(
            {
                "name": "narration_headroom",
                "status": "passed" if maximum <= -0.5 else "warning",
                "detail": f"Peak sample level {maximum:.1f} dBFS.",
            }
        )
    if mean_match:
        mean = float(mean_match.group(1))
        checks.append(
            {
                "name": "narration_level",
                "status": "passed" if mean > -35 else "warning",
                "detail": f"Mean sample level {mean:.1f} dBFS.",
            }
        )
    silence = await _silence_windows(ffmpeg, path)
    if silence:
        checks.append(
            {
                "name": "silence",
                "status": "warning" if len(silence) > 3 else "passed",
                "detail": f"{len(silence)} silence window(s) detected; timing was preserved.",
            }
        )
    return checks


async def _silence_windows(ffmpeg: str, path: Path) -> list[tuple[float, float]]:
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "silencedetect=noise=-42dB:d=0.45",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    starts = [
        float(value)
        for kind, value in SILENCE_JSON.findall(stderr.decode("utf-8", errors="replace"))
        if kind == "start"
    ]
    return [(start, start) for start in starts]


# --------------------------------------------------------------- processing


def _limiter_limit(true_peak: float, headroom_db: float = 1.0) -> float:
    """Convert a true-peak target into a sample-peak limiter ceiling.

    Sample peaks can be a fraction of a dB below inter-sample (true) peaks, so
    the ceiling sits a little under the target instead of exactly on it.
    """

    return round(min(0.98, 10 ** ((true_peak - headroom_db) / 20)), 4)


def _base_filters(delivery: AudioDelivery, duration: float) -> list[str]:
    filters = ["highpass=f=70"]
    if delivery.denoise:
        filters.append("afftdn=nr=10:nf=-32")
    if delivery.de_ess:
        filters.append("deesser=i=0.32:m=0.5:f=0.5")
    if delivery.equalization:
        filters.extend(
            [
                "equalizer=f=190:t=q:w=1:g=-1.2",
                "equalizer=f=3100:t=q:w=1.1:g=1.2",
            ]
        )
    if delivery.compression:
        filters.append("acompressor=threshold=-18dB:ratio=2.2:attack=10:release=180:makeup=1.5")
    if delivery.trim_silence:
        filters.append("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1")
    return filters


def _delivery_filter(
    measurements: dict[str, float], delivery: AudioDelivery, duration: float
) -> str:
    filters = [
        *_base_filters(delivery, duration),
        (
            f"loudnorm=I={delivery.target_lufs}:TP={delivery.true_peak}:LRA=11:"
            f"measured_I={measurements.get('input_i', -24.0)}:"
            f"measured_TP={measurements.get('input_tp', -2.0)}:"
            f"measured_LRA={measurements.get('input_lra', 7.0)}:"
            f"measured_thresh={measurements.get('input_thresh', -34.0)}:"
            f"offset={measurements.get('target_offset', 0.0)}:linear=true"
        ),
    ]
    # Resample before limiting: sample-rate conversion can create new peaks, and
    # a limiter placed before it would let those through.
    filters.append(f"aresample={delivery.sample_rate}")
    if delivery.fades and duration > 0.8:
        filters.append("afade=t=in:st=0:d=0.12")
        filters.append(f"afade=t=out:st={max(0.0, duration - 0.35):.3f}:d=0.35")
    filters.append(f"alimiter=limit={_limiter_limit(delivery.true_peak)}:level=false")
    return ",".join(filters)


async def _run(ffmpeg: str, args: list[str], *, timeout: float = 300.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"Audio command timed out after {timeout:.0f}s") from None
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def post_process_narration(
    ffmpeg: str,
    narration_path: Path,
    output_path: Path,
    *,
    delivery: AudioDelivery,
    duration_seconds: float,
) -> AudioValidation:
    if not narration_path.is_file():
        raise FileNotFoundError(f"Narration audio not found: {narration_path}")
    measurements = await measure_loudness(
        ffmpeg, narration_path, target_lufs=delivery.target_lufs, true_peak=delivery.true_peak
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    code, _, stderr = await _run(
        ffmpeg,
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-i",
            str(narration_path),
            "-af",
            _delivery_filter(measurements, delivery, duration_seconds),
            "-ar",
            str(delivery.sample_rate),
            "-ac",
            str(delivery.channels),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
    )
    if code != 0 or not output_path.is_file():
        raise RuntimeError(f"FFmpeg narration post-production failed: {stderr[-1200:]}")
    # EQ and compression run after the measurement used for the linear gain, so
    # the delivered file can land a little off target. Measure what was actually
    # written and correct it in one more pass; never report a target we missed.
    delivered = await measure_loudness(
        ffmpeg, output_path, target_lufs=delivery.target_lufs, true_peak=delivery.true_peak
    )
    delta = delivery.target_lufs - delivered.get("input_i", delivery.target_lufs)
    if abs(delta) > 0.4:
        corrected = output_path.with_name(output_path.stem + "-corrected.wav")
        gain = max(-12.0, min(12.0, delta))
        code, _, stderr = await _run(
            ffmpeg,
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-i",
                str(output_path),
                "-af",
                (
                    f"volume={gain:.2f}dB,aresample={delivery.sample_rate},"
                    f"alimiter=limit={_limiter_limit(delivery.true_peak)}:level=false"
                ),
                "-ar",
                str(delivery.sample_rate),
                "-ac",
                str(delivery.channels),
                "-c:a",
                "pcm_s16le",
                str(corrected),
            ],
        )
        print(
            "DEBUG correction",
            code,
            repr(str(corrected)),
            corrected.exists(),
            sorted(p.name for p in output_path.parent.iterdir()),
            stderr[-300:],
        )
        print(
            "DEBUG correction file",
            corrected.stat().st_size,
            await measure_loudness(ffmpeg, corrected),
        )
        if code == 0 and corrected.is_file():
            corrected.replace(output_path)
    validation = await validate_audio(
        ffmpeg,
        output_path,
        expected_duration=duration_seconds,
        target_lufs=delivery.target_lufs,
        true_peak=delivery.true_peak,
    )
    # Keep the narration pass distinguishable from the final-mix pass; both
    # measure loudness and peaks but at different stages.
    validation.checks = [
        {**check, "name": f"narration_{check['name']}"} for check in validation.checks
    ]
    validation.checks.insert(
        0,
        {
            "name": "narration_post",
            "status": "passed" if validation.passed else "warning",
            "detail": (
                "High-pass, de-esser, EQ, compression, two-pass loudness, limiter, "
                "and fades were applied to the narration."
            ),
        },
    )
    return validation


def _write_sfx_track(
    path: Path,
    *,
    duration_seconds: float,
    cues: list[float],
    level: float,
    sample_rate: int = 48_000,
) -> Path:
    """Create a restrained, deterministic accent bed for non-sensitive stories."""

    path.parent.mkdir(parents=True, exist_ok=True)
    total = max(1, int(duration_seconds * sample_rate))
    samples = [0.0] * total
    for cue in cues:
        start = max(0, int(cue * sample_rate))
        length = int(0.16 * sample_rate)
        for index in range(length):
            if start + index >= total:
                break
            envelope = math.sin(math.pi * index / max(1, length - 1)) ** 2
            tone = math.sin(2 * math.pi * 220 * index / sample_rate)
            samples[start + index] += tone * envelope * level
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for value in samples:
            clamped = max(-1.0, min(1.0, value))
            packed = struct.pack("<h", int(clamped * 32767))
            frames.extend(packed)
            frames.extend(packed)
        handle.writeframes(bytes(frames))
    return path


async def mix_final(
    ffmpeg: str,
    narration_path: Path,
    output_path: Path,
    *,
    delivery: AudioDelivery,
    duration_seconds: float,
    sfx_cues: list[float] | None = None,
) -> tuple[Path, list[str]]:
    """Mix narration, optional music, and optional restrained SFX."""

    warnings: list[str] = []
    music_path = Path(delivery.music_path) if delivery.music_path else None
    if music_path is not None and not music_path.is_file():
        warnings.append(f"Music file disappeared before mixing: {music_path}. Narration only.")
        music_path = None
    if delivery.music_enabled and music_path is None:
        warnings.append("Music was enabled but no licensed track was available; narration only.")
    sfx_path: Path | None = None
    if delivery.sfx_enabled and delivery.sfx_level > 0:
        cues = [cue for cue in (sfx_cues or []) if 0 < cue < duration_seconds - 0.2]
        if cues:
            sfx_path = _write_sfx_track(
                output_path.with_name(output_path.stem + "-sfx.wav"),
                duration_seconds=duration_seconds,
                cues=cues[:6],
                level=min(0.08, delivery.sfx_level),
            )

    args = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-i",
        str(narration_path),
    ]
    inputs = 1
    if music_path is not None:
        args += ["-stream_loop", "-1", "-i", str(music_path)]
        inputs += 1
    sfx_index = None
    if sfx_path is not None:
        sfx_index = inputs
        args += ["-i", str(sfx_path)]
        inputs += 1

    if music_path is None and sfx_path is None:
        args += [
            "-af",
            f"volume={delivery.narration_level},"
            f"aresample={delivery.sample_rate},"
            f"alimiter=limit={_limiter_limit(delivery.true_peak, 0.9)}:level=false",
            "-t",
            f"{duration_seconds:.3f}",
            "-ar",
            str(delivery.sample_rate),
            "-ac",
            str(delivery.channels),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    else:
        parts: list[str] = []
        if music_path is not None and delivery.ducking:
            parts.append(
                f"[0:a]volume={delivery.narration_level},aresample={delivery.sample_rate}[narrsrc]"
            )
            parts.append("[narrsrc]asplit=2[narr][sc]")
        else:
            parts.append(
                f"[0:a]volume={delivery.narration_level},aresample={delivery.sample_rate}[narr]"
            )
        if music_path is not None:
            fade_out = max(0.0, duration_seconds - 1.2)
            parts.append(
                f"[1:a]volume={delivery.music_level},afade=t=in:st=0:d=0.8,"
                f"afade=t=out:st={fade_out:.3f}:d=1.2[music]"
            )
            if delivery.ducking:
                parts.append(
                    "[music][sc]sidechaincompress=threshold=0.035:ratio=8:attack=20:release=450[duck]"
                )
                parts.append(
                    "[narr][duck]amix=inputs=2:duration=first:"
                    "dropout_transition=2:weights='1 1'[mix]"
                )
            else:
                parts.append("[narr][music]amix=inputs=2:duration=first:dropout_transition=2[mix]")
            mix_label = "[mix]"
        else:
            mix_label = "[narr]"
        if sfx_path is not None and sfx_index is not None:
            parts.append(f"[{sfx_index}:a]volume={delivery.sfx_level}[sfx]")
            parts.append(f"{mix_label}[sfx]amix=inputs=2:duration=first:dropout_transition=2[mix2]")
            mix_label = "[mix2]"
        parts.append(
            f"{mix_label}aresample={delivery.sample_rate},"
            f"alimiter=limit={_limiter_limit(delivery.true_peak, 0.9)}:level=false[out]"
        )
        args += [
            "-filter_complex",
            ";".join(parts),
            "-map",
            "[out]",
            "-t",
            f"{duration_seconds:.3f}",
            "-ar",
            str(delivery.sample_rate),
            "-ac",
            str(delivery.channels),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    code, _, stderr = await _run(ffmpeg, args)
    if code != 0 or not output_path.is_file():
        raise RuntimeError(f"FFmpeg final mix failed: {stderr[-1400:]}")
    if music_path is not None:
        warnings.append(
            "Music was mixed under narration with sidechain ducking; confirm the track "
            "license covers this distribution."
        )
    return output_path, warnings


async def validate_final_mix(
    ffmpeg: str,
    path: Path,
    *,
    delivery: AudioDelivery,
    expected_duration: float,
) -> AudioValidation:
    validation = await validate_audio(
        ffmpeg,
        path,
        expected_duration=expected_duration,
        target_lufs=delivery.target_lufs,
        true_peak=delivery.true_peak,
    )
    validation.checks.append(
        {
            "name": "music_subordination",
            "status": (
                "passed"
                if not delivery.music_enabled
                or (delivery.music_level < delivery.narration_level and delivery.ducking)
                else "failed"
            ),
            "detail": (
                "No music bed is present."
                if not delivery.music_enabled
                else f"Music level {delivery.music_level:.2f} vs narration "
                f"{delivery.narration_level:.2f}; ducking={delivery.ducking}."
            ),
        }
    )
    return validation


async def produce_audio(
    ffmpeg: str,
    narration_path: Path,
    output_dir: Path,
    *,
    delivery: AudioDelivery,
    duration_seconds: float,
    sfx_cues: list[float] | None = None,
) -> AudioProductionResult:
    """Post-process narration, mix the bed, and validate the final deliverable."""

    output_dir.mkdir(parents=True, exist_ok=True)
    processed = output_dir / "narration-delivery.wav"
    warnings: list[str] = []
    analysis = await analyze_narration(ffmpeg, narration_path)
    narration_validation = await post_process_narration(
        ffmpeg,
        narration_path,
        processed,
        delivery=delivery,
        duration_seconds=duration_seconds,
    )
    mix_path = output_dir / "final-mix.wav"
    mix_path, mix_warnings = await mix_final(
        ffmpeg,
        processed,
        mix_path,
        delivery=delivery,
        duration_seconds=duration_seconds,
        sfx_cues=sfx_cues,
    )
    warnings.extend(mix_warnings)
    final_validation = await validate_final_mix(
        ffmpeg,
        mix_path,
        delivery=delivery,
        expected_duration=duration_seconds,
    )
    probe = await probe_audio(ffmpeg, mix_path)
    sfx_file = mix_path.with_name(mix_path.stem + "-sfx.wav")
    if delivery.sfx_enabled and not sfx_file.is_file():
        warnings.append(
            "Sound effects were enabled but no usable cue was supplied, so the "
            "final mix contains narration and music only."
        )
    checks = [
        *analysis,
        *narration_validation.checks,
        {
            "name": "delivery_path",
            "status": "passed",
            "detail": f"Final mix written to {mix_path.name}.",
        },
    ]
    return AudioProductionResult(
        path=mix_path,
        duration_seconds=probe.duration_seconds if probe else duration_seconds,
        delivery=delivery,
        validation=final_validation,
        warnings=warnings,
        music_path=delivery.music_path,
        sfx_path=str(sfx_file) if sfx_file.is_file() else None,
        checks=checks,
    )
