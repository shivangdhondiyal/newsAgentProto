from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image

from backend.app.media.audio import ffprobe_for
from backend.app.media.craft import FFMPEG_TRANSITIONS

MOTION_PROFILES = {
    "static",
    "slow_zoom",
    "slow_pan",
    "ken_burns",
    "parallax",
    "punch",
    "map_drift",
}


@dataclass(frozen=True)
class SceneClip:
    """One composed still and the exact narration slot it must occupy."""

    position: int
    image_path: Path
    duration_seconds: float
    transition: str = "cut"
    motion: str = "static"
    motion_intensity: float = 0.2
    easing: str = "ease_in_out"
    motion_path: str = "linear"


@dataclass
class RenderCheck:
    name: str
    status: str
    detail: str


@dataclass
class RenderResult:
    path: Path
    width: int
    height: int
    duration_seconds: float
    size_bytes: int
    checks: list[RenderCheck] = field(default_factory=list)
    transition_warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.status != "failed" for check in self.checks)

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "width": self.width,
            "height": self.height,
            "duration_seconds": round(self.duration_seconds, 3),
            "size_bytes": self.size_bytes,
            "passed": self.passed,
            "checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.checks
            ],
            "transition_warnings": self.transition_warnings,
        }


class FFmpegSceneRenderer:
    """Compose scene frames into a real, aspect-specific MP4 with synchronized audio."""

    name = "ffmpeg-scenes"

    def __init__(
        self,
        executable: str,
        *,
        fps: int = 30,
        crf: int = 20,
        timeout_seconds: float = 900.0,
        scratch_dir: Path | None = None,
    ) -> None:
        self._executable = executable
        self._fps = max(1, int(fps))
        self._crf = max(0, min(51, int(crf)))
        self._timeout_seconds = max(30.0, float(timeout_seconds))
        self._scratch_dir = scratch_dir

    def _work_path(self, output_path: Path) -> Path:
        if self._scratch_dir is None:
            return output_path
        self._scratch_dir.mkdir(parents=True, exist_ok=True)
        return self._scratch_dir / f"{output_path.stem}-{uuid4().hex}.mp4"

    async def _encode(self, command: list[str], output_path: Path, *, timeout: float) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            # A wedged encoder must never hold a production job open forever.
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
            raise RuntimeError(
                f"FFmpeg scene render timed out after {timeout:.0f}s for {output_path}."
            ) from exc
        if process.returncode != 0 or not output_path.is_file():
            message = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"FFmpeg scene render failed: {message[-1600:]}")

    @staticmethod
    async def _publish(work_path: Path, output_path: Path) -> None:
        if work_path == output_path:
            return
        output_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: OSError | None = None
        for delay in (0.0, 0.5, 1.0, 2.0, 4.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                await asyncio.to_thread(shutil.move, str(work_path), str(output_path))
                return
            except OSError as exc:  # a scanner or sync client may hold the target
                last_error = exc
        raise RuntimeError(
            f"Rendered video could not be moved into {output_path}: {last_error}"
        )

    async def render(
        self,
        clips: list[SceneClip],
        narration_path: Path,
        subtitle_path: Path,
        output_path: Path,
        duration_seconds: float,
        *,
        width: int,
        height: int,
        transition_seconds: float = 0.4,
        aspect_ratio: str = "16:9",
    ) -> RenderResult:
        if not clips:
            raise ValueError("At least one scene clip is required")
        if not narration_path.is_file():
            raise FileNotFoundError(f"Narration audio not found: {narration_path}")
        for clip in clips:
            if not clip.image_path.is_file():
                raise FileNotFoundError(f"Scene frame not found: {clip.image_path}")
            try:
                with Image.open(clip.image_path) as image:
                    image.verify()
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"Scene frame is not a decodable image: {clip.image_path}"
                ) from exc

        width = _even(max(64, int(width)))
        height = _even(max(64, int(height)))
        total = max(0.5, float(duration_seconds))
        clips = _fit_clips_to_duration(clips, total)
        transition = max(0.04, min(1.5, float(transition_seconds)))
        if len(clips) == 1:
            transition = 0.0

        output_path.parent.mkdir(parents=True, exist_ok=True)
        input_args: list[str] = []
        for clip in clips:
            # Extra tail room lets the outgoing clip survive its crossfade.
            length = clip.duration_seconds + (transition if len(clips) > 1 else 0.0)
            input_args += [
                "-loop",
                "1",
                "-framerate",
                str(self._fps),
                "-t",
                f"{length:.3f}",
                "-i",
                str(clip.image_path),
            ]
        audio_index = len(clips)
        input_args += ["-i", str(narration_path)]
        subtitle_index: int | None = None
        if subtitle_path.is_file():
            subtitle_index = len(clips) + 1
            input_args += ["-i", str(subtitle_path)]

        filter_parts: list[str] = []
        for index, clip in enumerate(clips):
            length = clip.duration_seconds + (transition if len(clips) > 1 else 0.0)
            filter_parts.append(
                _scene_filter(index, clip, length, width, height, self._fps, aspect_ratio)
            )

        if len(clips) == 1:
            video_label = "[v0]"
        else:
            current = "[v0]"
            slot_start = 0.0
            for index in range(1, len(clips)):
                slot_start += clips[index - 1].duration_seconds
                offset = max(0.0, slot_start - transition)
                transition_name = clips[index].transition
                duration = transition
                if transition_name == "cut":
                    transition_name = "fade"
                    duration = 0.04
                if transition_name not in FFMPEG_TRANSITIONS:
                    transition_name = "fade"
                out_label = f"[vx{index}]"
                filter_parts.append(
                    f"{current}[v{index}]xfade=transition={transition_name}:"
                    f"duration={duration:.3f}:offset={offset:.3f}{out_label}"
                )
                current = out_label
            video_label = current

        filter_parts.append(f"{video_label}format=yuv420p[vout]")
        filter_complex = ";".join(filter_parts)

        head = [
            self._executable,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *input_args,
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            f"{audio_index}:a:0",
        ]
        if subtitle_index is not None:
            head += ["-map", f"{subtitle_index}:s:0", "-c:s", "mov_text"]
        tail = [
            "-t",
            f"{total:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(self._crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(self._fps),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]

        # Encode into a scratch directory under a unique name, then move the
        # finished file into the media folder. Writing straight into a
        # cloud-synced or antivirus-scanned path can block the encoder while it
        # relocates the moov atom for `+faststart`, leaving a truncated file.
        work_path = self._work_path(output_path)
        for attempt in (1, 2):
            faststart = attempt == 1
            command = [
                *head,
                *tail,
                *(["-movflags", "+faststart"] if faststart else []),
                # `-t` already bounds the output. `-shortest` would instead stop
                # at the final subtitle cue, clipping the end of the narration.
                str(work_path),
            ]
            try:
                await self._encode(command, work_path, timeout=self._timeout_seconds)
                break
            except RuntimeError as exc:
                with suppress(OSError):
                    work_path.unlink(missing_ok=True)
                if attempt == 2:
                    raise RuntimeError(
                        f"{exc} Faststart was retried without the moov-atom "
                        "relocation pass and still failed."
                    ) from exc

        await self._publish(work_path, output_path)

        result = await validate_render(
            self._executable,
            output_path,
            expected_width=width,
            expected_height=height,
            expected_duration=total,
        )
        result.transition_warnings = _transition_warnings(clips)
        return result


def _scene_filter(
    index: int,
    clip: SceneClip,
    length: float,
    width: int,
    height: int,
    fps: int,
    aspect_ratio: str,
) -> str:
    frames = max(1, round(length * fps))
    motion = clip.motion if clip.motion in MOTION_PROFILES else "static"
    base = (
        f"[{index}:v]scale={width * 2}:{height * 2}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},setsar=1"
    )
    if motion == "static":
        return (
            f"{base},scale={width}:{height},fps={fps},"
            f"trim=duration={length:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
        )

    intensity = max(0.0, min(1.0, clip.motion_intensity))
    amp = 0.02 + intensity * 0.12
    if aspect_ratio == "9:16":
        amp *= 0.7
    progress = f"(on/{frames})"
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    zoom = f"1+{amp:.4f}*{progress}"
    x_expr = center_x
    y_expr = center_y

    if motion in {"slow_pan", "ken_burns", "parallax"}:
        direction = 1.0 if clip.position % 2 else -1.0
        pan = 0.02 + intensity * 0.05
        x_expr = f"{center_x}+{direction * pan:.4f}*iw*({progress}-0.5)"
    if motion == "map_drift":
        pan = 0.02 + intensity * 0.04
        x_expr = f"{center_x}-{pan:.4f}*iw*{progress}"
        y_expr = f"{center_y}+0.01*ih*({progress}-0.5)"
    if motion == "punch":
        zoom = f"1+{amp * 1.4:.4f}*{progress}"

    return (
        f"{base},zoompan=z='{zoom}':x='{x_expr}':y='{y_expr}':d=1:"
        f"s={width}x{height}:fps={fps},"
        f"trim=duration={length:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
    )


def _fit_clips_to_duration(clips: list[SceneClip], total: float) -> list[SceneClip]:
    """Keep the narration timeline exact; absorb rounding in the final scene."""

    duration_sum = sum(max(0.2, clip.duration_seconds) for clip in clips)
    if duration_sum <= 0:
        raise ValueError("Scene durations must be positive")
    scale = total / duration_sum
    fitted: list[SceneClip] = []
    for clip in clips:
        fitted.append(
            SceneClip(
                position=clip.position,
                image_path=clip.image_path,
                duration_seconds=max(0.2, clip.duration_seconds * scale),
                transition=clip.transition,
                motion=clip.motion,
                motion_intensity=clip.motion_intensity,
                easing=clip.easing,
                motion_path=clip.motion_path,
            )
        )
    return fitted


def _transition_warnings(clips: list[SceneClip]) -> list[str]:
    if len(clips) <= 1:
        return []
    decorative = [
        clip
        for clip in clips[1:]
        if clip.transition not in {"cut", "fade", "dissolve", "wipeleft", "wiperight"}
    ]
    ratio = len(decorative) / max(1, len(clips) - 1)
    if ratio > 0.35:
        return [
            (
                f"{round(ratio * 100)}% of scene changes use decorative transitions. "
                "The editing guide recommends hard cuts for most changes."
            )
        ]
    return []


def _even(value: int) -> int:
    return value if value % 2 == 0 else value - 1


async def validate_render(
    ffmpeg: str,
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
    expected_duration: float,
) -> RenderResult:
    checks: list[RenderCheck] = []
    size_bytes = path.stat().st_size if path.is_file() else 0
    checks.append(
        RenderCheck(
            name="file_exists",
            status="passed" if size_bytes > 1024 else "failed",
            detail=f"Rendered file is {size_bytes} bytes.",
        )
    )
    payload: dict[str, Any] = {}
    ffprobe = ffprobe_for(ffmpeg)
    if ffprobe is None:
        checks.append(
            RenderCheck(
                name="stream_probe",
                status="warning",
                detail="ffprobe is unavailable; stream checks were skipped.",
            )
        )
    else:
        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,sample_rate,channels:format=duration,size",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0 and stdout.strip():
            payload = json.loads(stdout.decode("utf-8", errors="replace"))

    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if video is None:
        checks.append(
            RenderCheck(name="video_stream", status="failed", detail="No video stream found.")
        )
    else:
        matches = (
            int(video.get("width") or 0) == expected_width
            and int(video.get("height") or 0) == expected_height
        )
        checks.append(
            RenderCheck(
                name="frame_size",
                status="passed" if matches else "failed",
                detail=(
                    f"{video.get('width')}x{video.get('height')} "
                    f"({video.get('codec_name')}); expected "
                    f"{expected_width}x{expected_height}."
                ),
            )
        )
    checks.append(
        RenderCheck(
            name="audio_stream",
            status="passed" if audio is not None else "failed",
            detail=(
                f"{audio.get('codec_name')} · {audio.get('sample_rate')} Hz · "
                f"{audio.get('channels')} channel(s)"
                if audio
                else "No audio stream found."
            ),
        )
    )
    duration = float(payload.get("format", {}).get("duration") or 0.0)
    if duration:
        drift = abs(duration - expected_duration)
        checks.append(
            RenderCheck(
                name="duration_sync",
                status="passed" if drift <= 1.0 else "warning",
                detail=(
                    f"Video {duration:.2f}s vs narration {expected_duration:.2f}s "
                    f"(drift {drift:.2f}s)."
                ),
            )
        )
    return RenderResult(
        path=path,
        width=expected_width,
        height=expected_height,
        duration_seconds=duration or expected_duration,
        size_bytes=size_bytes,
        checks=checks,
    )
