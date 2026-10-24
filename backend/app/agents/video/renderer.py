from __future__ import annotations

import asyncio
from pathlib import Path


class FFmpegVideoRenderer:
    """Deterministic FFmpeg renderer with a neutral background and selectable subtitle track."""

    name = "ffmpeg"

    def __init__(
        self,
        executable: str,
        width: int,
        height: int,
        fps: int,
        background_color: str,
    ) -> None:
        self._executable = executable
        self._width = width
        self._height = height
        self._fps = fps
        self._background_color = background_color

    async def render(
        self,
        narration_path: Path,
        subtitle_path: Path,
        output_path: Path,
        duration_seconds: float,
    ) -> None:
        if not narration_path.is_file():
            raise FileNotFoundError(f"Narration WAV not found: {narration_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        color_source = (
            f"color=c={self._background_color}:s={self._width}x{self._height}:r={self._fps}"
        )
        process = await asyncio.create_subprocess_exec(
            self._executable,
            "-y",
            "-f",
            "lavfi",
            "-i",
            color_source,
            "-i",
            str(narration_path),
            "-i",
            str(subtitle_path),
            "-t",
            f"{duration_seconds:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-c:s",
            "mov_text",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not output_path.is_file():
            raise RuntimeError(f"FFmpeg render failed: {stderr.decode('utf-8', errors='replace')}")
