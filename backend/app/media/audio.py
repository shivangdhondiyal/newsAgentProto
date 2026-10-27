from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOUDNESS_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


@dataclass(frozen=True)
class AudioProbe:
    duration_seconds: float
    sample_rate: int
    channels: int
    codec: str
    bit_rate: int | None = None


@dataclass
class AudioValidation:
    checks: list[dict[str, str]] = field(default_factory=list)
    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None

    @property
    def passed(self) -> bool:
        return all(check["status"] != "failed" for check in self.checks)


def resolve_executable(executable: str) -> str | None:
    path = Path(executable)
    if path.is_file():
        return str(path)
    found = shutil.which(executable)
    return found


def ffprobe_for(ffmpeg: str) -> str | None:
    path = Path(ffmpeg)
    if path.is_file():
        candidate = path.with_name("ffprobe.exe" if path.suffix.lower() == ".exe" else "ffprobe")
        if candidate.is_file():
            return str(candidate)
        sibling = path.with_name("ffprobe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


async def _run(command: list[str], *, timeout: float = 180.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"Command timed out after {timeout:.0f}s: {command[0]}") from None
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def probe_audio(ffmpeg: str, path: Path) -> AudioProbe | None:
    ffprobe = ffprobe_for(ffmpeg)
    if ffprobe is None:
        return None
    code, stdout, _ = await _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels,codec_name,bit_rate:format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if code != 0 or not stdout.strip():
        return None
    payload = json.loads(stdout)
    streams = payload.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    duration = payload.get("format", {}).get("duration") or stream.get("duration") or 0
    try:
        bit_rate = int(stream["bit_rate"]) if stream.get("bit_rate") else None
    except (TypeError, ValueError):
        bit_rate = None
    return AudioProbe(
        duration_seconds=float(duration or 0),
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        codec=str(stream.get("codec_name") or ""),
        bit_rate=bit_rate,
    )


async def measure_loudness(
    ffmpeg: str,
    path: Path,
    *,
    target_lufs: float = -16.0,
    true_peak: float = -1.5,
) -> dict[str, float]:
    code, _, stderr = await _run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout=180.0,
    )
    matches = LOUDNESS_JSON.findall(stderr)
    if code != 0 or not matches:
        raise RuntimeError("FFmpeg loudness measurement did not return JSON")
    payload = json.loads(matches[-1])
    result: dict[str, float] = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            result[key] = float(payload.get(key))
        except (TypeError, ValueError):
            result[key] = 0.0
    return result


def _loudnorm_filter(measurements: dict[str, float], target_lufs: float, true_peak: float) -> str:
    return (
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11:"
        f"measured_I={measurements.get('input_i', -24.0)}:"
        f"measured_TP={measurements.get('input_tp', -2.0)}:"
        f"measured_LRA={measurements.get('input_lra', 7.0)}:"
        f"measured_thresh={measurements.get('input_thresh', -34.0)}:"
        f"offset={measurements.get('target_offset', 0.0)}:linear=true"
    )


async def master_narration(
    ffmpeg: str,
    raw_path: Path,
    output_path: Path,
    *,
    target_lufs: float = -16.0,
    true_peak: float = -1.5,
    sample_rate: int = 48_000,
    channels: int = 2,
    duration_seconds: float | None = None,
) -> AudioValidation:
    """Two-pass EBU R128 mastering with EQ, gentle compression, limiting, and fades."""

    if not raw_path.is_file():
        raise FileNotFoundError(f"Narration audio not found: {raw_path}")
    probe = await probe_audio(ffmpeg, raw_path)
    duration = duration_seconds or (probe.duration_seconds if probe else 0.0)
    measurements = await measure_loudness(
        ffmpeg, raw_path, target_lufs=target_lufs, true_peak=true_peak
    )
    fade_out = max(0.0, duration - 0.45)
    filters = [
        "highpass=f=70",
        "equalizer=f=180:t=q:w=1:g=-1.5",
        "equalizer=f=3200:t=q:w=1.2:g=1.5",
        "acompressor=threshold=-18dB:ratio=2.5:attack=8:release=180:makeup=2",
        _loudnorm_filter(measurements, target_lufs, true_peak),
        "alimiter=limit=0.891",
        "afade=t=in:st=0:d=0.15",
        f"afade=t=out:st={fade_out:.3f}:d=0.45",
        f"aresample={sample_rate}",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    code, _, stderr = await _run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-i",
            str(raw_path),
            "-af",
            ",".join(filters),
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        timeout=300.0,
    )
    if code != 0 or not output_path.is_file():
        raise RuntimeError(f"FFmpeg audio mastering failed: {stderr[-1200:]}")
    validation = await validate_audio(
        ffmpeg,
        output_path,
        expected_duration=duration,
        target_lufs=target_lufs,
        true_peak=true_peak,
    )
    validation.checks.insert(
        0,
        {
            "name": "loudness_mastering",
            "status": "passed" if validation.passed else "warning",
            "detail": (
                f"Two-pass EBU R128 target {target_lufs:.1f} LUFS, "
                f"true peak {true_peak:.1f} dBTP, EQ, compression, limiter, fades."
            ),
        },
    )
    return validation


async def validate_audio(
    ffmpeg: str,
    path: Path,
    *,
    expected_duration: float | None = None,
    target_lufs: float | None = None,
    true_peak: float | None = None,
) -> AudioValidation:
    result = AudioValidation()
    if not path.is_file():
        result.checks.append(
            {"name": "file_exists", "status": "failed", "detail": "Audio file is missing."}
        )
        return result
    probe = await probe_audio(ffmpeg, path)
    if probe is None:
        result.checks.append(
            {
                "name": "stream_probe",
                "status": "warning",
                "detail": "ffprobe is unavailable; stream checks were skipped.",
            }
        )
    else:
        result.duration_seconds = probe.duration_seconds
        result.sample_rate = probe.sample_rate
        result.channels = probe.channels
        result.checks.append(
            {
                "name": "stream_layout",
                "status": "passed" if probe.channels in {1, 2} else "warning",
                "detail": (
                    f"{probe.codec or 'audio'} · {probe.sample_rate} Hz · "
                    f"{probe.channels} channel(s)"
                ),
            }
        )
        if expected_duration:
            drift = abs(probe.duration_seconds - expected_duration)
            result.checks.append(
                {
                    "name": "duration_alignment",
                    "status": "passed" if drift <= 1.5 else "failed",
                    "detail": (
                        f"Audio duration {probe.duration_seconds:.2f}s vs "
                        f"expected {expected_duration:.2f}s (drift {drift:.2f}s)."
                    ),
                }
            )
    try:
        measurements = await measure_loudness(
            ffmpeg,
            path,
            target_lufs=target_lufs or -16.0,
            true_peak=true_peak or -1.5,
        )
        result.integrated_lufs = measurements["input_i"]
        result.true_peak_dbtp = measurements["input_tp"]
        if target_lufs is not None:
            delta = abs(measurements["input_i"] - target_lufs)
            result.checks.append(
                {
                    "name": "integrated_loudness",
                    "status": "passed" if delta <= 2.0 else "warning",
                    "detail": (
                        f"{measurements['input_i']:.1f} LUFS vs target {target_lufs:.1f} LUFS."
                    ),
                }
            )
        result.checks.append(
            {
                "name": "true_peak",
                "status": "passed" if measurements["input_tp"] <= -0.9 else "failed",
                "detail": f"True peak {measurements['input_tp']:.2f} dBTP.",
            }
        )
        if measurements["input_i"] < -45:
            result.checks.append(
                {
                    "name": "silence",
                    "status": "failed",
                    "detail": "Audio is effectively silent.",
                }
            )
    except RuntimeError as exc:
        result.checks.append(
            {"name": "loudness_measurement", "status": "warning", "detail": str(exc)}
        )
    return result


async def mix_music(
    ffmpeg: str,
    narration_path: Path,
    music_path: Path,
    output_path: Path,
    *,
    duration_seconds: float,
    music_gain: float = 0.12,
    ducking: bool = True,
) -> Path:
    """Mix a user-supplied licensed track under narration with sidechain ducking."""

    if not music_path.is_file():
        raise FileNotFoundError(f"Music track not found: {music_path}")
    fade_out = max(0.0, duration_seconds - 1.5)
    if ducking:
        filter_complex = (
            f"[1:a]volume={music_gain},afade=t=in:st=0:d=1.0,"
            f"afade=t=out:st={fade_out:.3f}:d=1.5[music];"
            "[music][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=500[duck];"
            "[0:a][duck]amix=inputs=2:duration=first:dropout_transition=2[out]"
        )
    else:
        filter_complex = (
            f"[1:a]volume={music_gain},afade=t=in:st=0:d=1.0,"
            f"afade=t=out:st={fade_out:.3f}:d=1.5[music];"
            "[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[out]"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    code, _, stderr = await _run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-i",
            str(narration_path),
            "-stream_loop",
            "-1",
            "-i",
            str(music_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-t",
            f"{duration_seconds:.3f}",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        timeout=300.0,
    )
    if code != 0 or not output_path.is_file():
        raise RuntimeError(f"FFmpeg music mix failed: {stderr[-1200:]}")
    return output_path


async def probe_media(ffmpeg: str, path: Path) -> dict[str, Any]:
    ffprobe = ffprobe_for(ffmpeg)
    if ffprobe is None:
        return {}
    code, stdout, _ = await _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,sample_rate,channels,duration,r_frame_rate",
            "-show_entries",
            "format=duration,size,format_name",
            "-of",
            "json",
            str(path),
        ]
    )
    if code != 0 or not stdout.strip():
        return {}
    return json.loads(stdout)
