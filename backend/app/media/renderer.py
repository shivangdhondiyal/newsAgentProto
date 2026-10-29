from __future__ import annotations

import asyncio
import json
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image

from backend.app.media.audio import ffprobe_for
from backend.app.media.craft import FFMPEG_TRANSITIONS
from backend.app.media.motion import OverlaySequence

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
    video_path: Path | None = None
    source_kind: str = "image"


@dataclass(frozen=True)
class CaptionRender:
    """One aspect's caption plan: burned-in ASS and/or a soft subtitle track.

    The caption service owns cue generation and ASS authoring; the renderer only
    places the finished files on the FFmpeg timeline. Keeping this small also
    means a renderer implementation can ignore captions without breaking the
    scene contract.
    """

    ass_path: Path | None = None
    fonts_dir: Path | None = None
    soft_subtitle_path: Path | None = None
    burn_in: bool = True
    soft_subs: bool = True
    cue_count: int = 0
    source: str = ""

    @property
    def burns_in(self) -> bool:
        return bool(self.burn_in and self.ass_path is not None)

    @property
    def adds_soft_track(self) -> bool:
        return bool(self.soft_subs and self.soft_subtitle_path is not None)

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "ass_path": str(self.ass_path) if self.ass_path else None,
            "soft_subtitle_path": str(self.soft_subtitle_path) if self.soft_subtitle_path else None,
            "burn_in": self.burns_in,
            "soft_subs": self.adds_soft_track,
            "cue_count": self.cue_count,
            "source": self.source,
        }


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
    caption: dict[str, Any] = field(default_factory=dict)
    qa: dict[str, Any] = field(default_factory=dict)
    contact_sheet_path: Path | None = None
    overlays: list[dict[str, Any]] = field(default_factory=list)

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
            "caption": self.caption,
            "qa": self.qa,
            "contact_sheet_path": str(self.contact_sheet_path) if self.contact_sheet_path else None,
            "overlays": self.overlays,
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
        raise RuntimeError(f"Rendered video could not be moved into {output_path}: {last_error}")

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
        caption_render: CaptionRender | None = None,
        overlays: list[OverlaySequence] | None = None,
        make_contact_sheet: bool = True,
    ) -> RenderResult:
        if not clips:
            raise ValueError("At least one scene clip is required")
        if not narration_path.is_file():
            raise FileNotFoundError(f"Narration audio not found: {narration_path}")
        overlays = list(overlays or [])
        for clip in clips:
            if clip.video_path is not None:
                if not clip.video_path.is_file():
                    raise FileNotFoundError(f"Scene video not found: {clip.video_path}")
                continue
            if not clip.image_path.is_file():
                raise FileNotFoundError(f"Scene frame not found: {clip.image_path}")
            try:
                with Image.open(clip.image_path) as image:
                    image.verify()
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"Scene frame is not a decodable image: {clip.image_path}"
                ) from exc
        for overlay in overlays:
            if not overlay.directory.is_dir():
                raise FileNotFoundError(
                    f"Motion-graphics overlay directory is missing: {overlay.directory}"
                )
            if not (overlay.directory / "frame-00000.png").is_file():
                raise FileNotFoundError(
                    f"Motion-graphics overlay has no rendered frames: {overlay.directory}"
                )
        if caption_render is not None and caption_render.burn_in:
            ass_path = caption_render.ass_path
            if ass_path is None:
                raise FileNotFoundError(
                    "Caption burn-in was requested without an ASS file. "
                    "Regenerate captions before rendering."
                )
            if not ass_path.is_file():
                raise FileNotFoundError(
                    f"Caption subtitle file not found: {ass_path}. "
                    "Regenerate captions before rendering."
                )
        soft_path = subtitle_path
        if caption_render is not None:
            soft_path = caption_render.soft_subtitle_path if caption_render.soft_subs else None
            if caption_render.soft_subs and soft_path is not None and not soft_path.is_file():
                raise FileNotFoundError(
                    f"Caption sidecar file not found: {soft_path}. "
                    "Regenerate captions before rendering."
                )

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
            if clip.video_path is not None:
                input_args += [
                    "-stream_loop",
                    "-1",
                    "-t",
                    f"{length:.3f}",
                    "-i",
                    str(clip.video_path),
                ]
            else:
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
        next_index = len(clips) + 1
        subtitle_index: int | None = None
        if soft_path is not None and soft_path.is_file():
            subtitle_index = next_index
            input_args += ["-i", str(soft_path)]
            next_index += 1
        overlay_indices: list[int] = []
        for overlay in overlays:
            overlay_indices.append(next_index)
            input_args += [
                "-framerate",
                str(overlay.fps),
                "-start_number",
                "0",
                "-i",
                str(overlay.pattern),
            ]
            next_index += 1

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

        # Motion graphics are composited under burned-in captions so the caption
        # layer always wins a collision. Each transparent PNG sequence keeps its
        # own absolute slot, independent of scene-render order.
        for position, overlay in enumerate(overlays):
            overlay_index = overlay_indices[position]
            start = max(0.0, overlay.start_seconds)
            end = start + max(0.04, overlay.duration_seconds)
            source_label = f"[ovsrc{position}]"
            filter_parts.append(
                f"[{overlay_index}:v]format=rgba,setpts=PTS-STARTPTS+{start:.3f}/TB{source_label}"
            )
            out_label = f"[vg{position}]"
            filter_parts.append(
                f"{video_label}{source_label}overlay=x=0:y=0:eof_action=pass:"
                f"enable='between(t,{start:.3f},{end:.3f})'{out_label}"
            )
            video_label = out_label

        if caption_render is not None and caption_render.burns_in:
            assert caption_render.ass_path is not None
            ass_filter = f"ass=filename='{_filter_path(caption_render.ass_path)}'"
            fonts_dir = caption_render.fonts_dir
            if fonts_dir is not None and fonts_dir.is_dir():
                ass_filter += f":fontsdir='{_filter_path(fonts_dir)}'"
            filter_parts.append(f"{video_label}{ass_filter}[vcaption]")
            video_label = "[vcaption]"

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

        contact_path: Path | None = None
        if make_contact_sheet:
            try:
                contact_path = await make_video_contact_sheet(
                    self._executable,
                    output_path,
                    output_path.with_name(output_path.stem + "-contact-sheet.jpg"),
                )
            except (OSError, RuntimeError):
                contact_path = None
        # A scene with an animated graphic is not a static scene even when its
        # base frame is a still panel: the overlay supplies the motion.
        animated: set[int] = set()
        cursor = 0.0
        for index, clip in enumerate(clips):
            end = cursor + clip.duration_seconds
            covered = sum(
                max(
                    0.0,
                    min(end, overlay.start_seconds + overlay.duration_seconds)
                    - max(cursor, overlay.start_seconds),
                )
                for overlay in overlays
            )
            if covered >= (end - cursor) * 0.5:
                animated.add(index)
            cursor = end
        static_ratio = sum(
            1
            for index, clip in enumerate(clips)
            if clip.motion == "static" and index not in animated
        ) / max(1, len(clips))
        result = await validate_render(
            self._executable,
            output_path,
            expected_width=width,
            expected_height=height,
            expected_duration=total,
            expect_subtitle_stream=subtitle_index is not None,
            static_scene_ratio=static_ratio,
            overlay_count=len(overlays),
            contact_sheet_path=contact_path,
        )
        result.transition_warnings = _transition_warnings(clips)
        if caption_render is not None:
            result.caption = caption_render.as_public_dict()
        result.overlays = [
            {
                "label": overlay.label,
                "start_seconds": round(overlay.start_seconds, 3),
                "duration_seconds": round(overlay.duration_seconds, 3),
                "frame_count": overlay.frame_count,
            }
            for overlay in overlays
        ]
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
    if clip.video_path is not None:
        # Real footage already carries motion. Reframing and a stable frame rate
        # are safer than adding a synthetic zoom that fights the source camera.
        return (
            f"{base},scale={width}:{height},fps={fps},"
            f"trim=duration={length:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
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
                video_path=clip.video_path,
                source_kind=clip.source_kind,
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


def _filter_path(path: Path) -> str:
    """Escape a path for an FFmpeg filter option.

    FFmpeg's filter parser treats ``:`` as an option separator and uses ``'``
    for quoting, so Windows drive letters and apostrophes must be escaped even
    though the surrounding path is quoted. Backslashes become forward slashes.
    """

    value = str(path).replace("\\", "/")
    value = value.replace("'", r"\'")
    return value.replace(":", r"\:")


async def validate_render(
    ffmpeg: str,
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
    expected_duration: float,
    expect_subtitle_stream: bool = False,
    static_scene_ratio: float | None = None,
    overlay_count: int = 0,
    contact_sheet_path: Path | None = None,
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
    subtitle = next((item for item in streams if item.get("codec_type") == "subtitle"), None)
    if expect_subtitle_stream:
        checks.append(
            RenderCheck(
                name="subtitle_stream",
                status="passed" if subtitle is not None else "failed",
                detail=(
                    f"Soft subtitle track present ({subtitle.get('codec_name')})."
                    if subtitle
                    else "The soft subtitle track was requested but is missing from the file."
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
    qa = await inspect_video_quality(ffmpeg, path)
    black = qa.get("black_intervals") or []
    frozen = qa.get("freeze_intervals") or []
    checks.append(
        RenderCheck(
            name="black_frames",
            status="warning" if black else "passed",
            detail=(
                f"{len(black)} black interval(s) longer than 0.5s detected."
                if black
                else "No long black intervals detected."
            ),
        )
    )
    checks.append(
        RenderCheck(
            name="frozen_frames",
            status="warning" if frozen else "passed",
            detail=(
                f"{len(frozen)} frozen interval(s) detected."
                if frozen
                else "No frozen intervals detected."
            ),
        )
    )
    if static_scene_ratio is not None:
        checks.append(
            RenderCheck(
                name="static_scene_ratio",
                status="warning" if static_scene_ratio > 0.65 else "passed",
                detail=(
                    f"{round(static_scene_ratio * 100)}% of scenes are static; "
                    "prefer footage or a meaningful camera move."
                ),
            )
        )
    if overlay_count:
        checks.append(
            RenderCheck(
                name="motion_graphics",
                status="passed",
                detail=f"{overlay_count} animated overlay sequence(s) composited.",
            )
        )
    if contact_sheet_path is not None and contact_sheet_path.is_file():
        checks.append(
            RenderCheck(
                name="contact_sheet",
                status="passed",
                detail=f"Inspection sheet written to {contact_sheet_path.name}.",
            )
        )
    return RenderResult(
        path=path,
        width=expected_width,
        height=expected_height,
        duration_seconds=duration or expected_duration,
        size_bytes=size_bytes,
        checks=checks,
        qa=qa,
        contact_sheet_path=contact_sheet_path,
    )


async def inspect_video_quality(ffmpeg: str, path: Path) -> dict[str, Any]:
    """Run FFmpeg's black/freeze detectors and return structured evidence."""

    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vf",
        "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-60dB:d=1.0",
        "-an",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    text = stderr.decode("utf-8", errors="replace")
    black = [
        {"start": float(start), "end": float(end), "duration": float(duration)}
        for start, end, duration in re.findall(
            r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)",
            text,
        )
    ]
    freeze = [
        {"start": float(start), "end": float(end), "duration": float(duration)}
        for start, end, duration in re.findall(
            r"freeze_start:\s*([0-9.]+)\s+freeze_end:\s*([0-9.]+)\s+freeze_duration:\s*([0-9.]+)",
            text,
        )
    ]
    return {
        "black_intervals": black,
        "freeze_intervals": freeze,
        "inspected_with": "ffmpeg blackdetect+freezedetect",
    }


async def make_video_contact_sheet(
    ffmpeg: str,
    video_path: Path,
    output_path: Path,
    *,
    columns: int = 4,
    rows: int = 3,
) -> Path:
    """Sample the finished video into a contact sheet for real visual inspection."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vf",
        f"fps=1/2,scale=480:-2,tile={columns}x{rows}:padding=4:margin=4",
        "-frames:v",
        "1",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0 or not output_path.is_file():
        raise RuntimeError(
            f"FFmpeg contact sheet failed: {stderr.decode('utf-8', errors='replace')[-800:]}"
        )
    return output_path
