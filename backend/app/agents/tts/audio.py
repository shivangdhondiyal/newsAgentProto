from __future__ import annotations

import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WaveMetadata:
    channels: int
    sample_width: int
    sample_rate: int
    frame_count: int

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate if self.sample_rate else 0.0


def wave_metadata(path: Path) -> WaveMetadata:
    with wave.open(str(path), "rb") as audio:
        return WaveMetadata(
            channels=audio.getnchannels(),
            sample_width=audio.getsampwidth(),
            sample_rate=audio.getframerate(),
            frame_count=audio.getnframes(),
        )


def merge_and_normalize_wav(inputs: list[Path], output_path: Path) -> WaveMetadata:
    """Merge PCM 16-bit WAV chunks and normalize their shared peak level locally."""

    if not inputs:
        raise ValueError("At least one WAV chunk is required")
    frames: list[bytes] = []
    expected: WaveMetadata | None = None
    for path in inputs:
        with wave.open(str(path), "rb") as audio:
            metadata = WaveMetadata(
                channels=audio.getnchannels(),
                sample_width=audio.getsampwidth(),
                sample_rate=audio.getframerate(),
                frame_count=audio.getnframes(),
            )
            if expected is None:
                expected = metadata
            elif (
                metadata.channels != expected.channels
                or metadata.sample_width != expected.sample_width
                or metadata.sample_rate != expected.sample_rate
            ):
                raise ValueError("All narration chunks must have matching WAV formats")
            frames.append(audio.readframes(metadata.frame_count))
    assert expected is not None
    if expected.sample_width != 2:
        raise ValueError("Only 16-bit PCM WAV output is supported by the local normalizer")
    merged = _normalize_16bit_pcm(b"".join(frames))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as output:
        output.setnchannels(expected.channels)
        output.setsampwidth(expected.sample_width)
        output.setframerate(expected.sample_rate)
        output.writeframes(merged)
    return WaveMetadata(
        channels=expected.channels,
        sample_width=expected.sample_width,
        sample_rate=expected.sample_rate,
        frame_count=len(merged) // (expected.channels * expected.sample_width),
    )


def merge_with_pauses(
    inputs: list[Path],
    pauses_ms: list[int],
    output_path: Path,
) -> tuple[WaveMetadata, list[float]]:
    """Merge chunks with explicit sentence/paragraph silence.

    Returns the merged WAV metadata and the real start time of every chunk, so
    subtitle and scene timing stay aligned with the audio that was actually
    produced rather than an estimate.
    """

    if not inputs:
        raise ValueError("At least one WAV chunk is required")
    if len(pauses_ms) != len(inputs):
        raise ValueError("Every chunk requires a matching pause value")
    expected: WaveMetadata | None = None
    starts: list[float] = []
    merged = bytearray()
    for index, path in enumerate(inputs):
        with wave.open(str(path), "rb") as audio:
            metadata = WaveMetadata(
                channels=audio.getnchannels(),
                sample_width=audio.getsampwidth(),
                sample_rate=audio.getframerate(),
                frame_count=audio.getnframes(),
            )
            if expected is None:
                expected = metadata
            elif (
                metadata.channels != expected.channels
                or metadata.sample_width != expected.sample_width
                or metadata.sample_rate != expected.sample_rate
            ):
                raise ValueError("All narration chunks must have matching WAV formats")
            if expected.sample_width != 2:
                raise ValueError("Only 16-bit PCM WAV output is supported")
            frame_bytes = audio.readframes(metadata.frame_count)
        starts.append(
            len(merged)
            / (expected.channels * expected.sample_width * expected.sample_rate)
        )
        merged.extend(frame_bytes)
        pause = max(0, int(pauses_ms[index]))
        if pause:
            silence_frames = round(expected.sample_rate * pause / 1_000)
            merged.extend(b"\x00" * silence_frames * expected.channels * expected.sample_width)
    assert expected is not None
    normalized = _normalize_16bit_pcm(bytes(merged))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as output:
        output.setnchannels(expected.channels)
        output.setsampwidth(expected.sample_width)
        output.setframerate(expected.sample_rate)
        output.writeframes(normalized)
    return (
        WaveMetadata(
            channels=expected.channels,
            sample_width=expected.sample_width,
            sample_rate=expected.sample_rate,
            frame_count=len(normalized) // (expected.channels * expected.sample_width),
        ),
        starts,
    )


def _normalize_16bit_pcm(frames: bytes) -> bytes:
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(sample) for sample in samples), default=0)
    if peak == 0:
        return frames
    gain = min(1.0, 29_490 / peak)
    if gain == 1.0:
        return frames
    normalized = array("h", (int(sample * gain) for sample in samples))
    if sys.byteorder != "little":
        normalized.byteswap()
    return normalized.tobytes()
