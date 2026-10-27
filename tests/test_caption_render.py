from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from backend.app.media.audio import resolve_executable
from backend.app.media.captions import (
    CaptionCue,
    default_fonts_dir,
    preset_settings,
    to_ass,
    to_srt,
)
from backend.app.media.renderer import (
    CaptionRender,
    FFmpegSceneRenderer,
    SceneClip,
    _filter_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_FFMPEG = REPO_ROOT / ".tools" / "ffmpeg" / "bin" / "ffmpeg.exe"


def _ffmpeg() -> str:
    candidate = str(LOCAL_FFMPEG) if LOCAL_FFMPEG.is_file() else resolve_executable("ffmpeg")
    if candidate is None:
        pytest.skip("FFmpeg is not installed")
    return candidate


def _fixtures(tmp_path: Path) -> tuple[Path, Path]:
    ffmpeg = _ffmpeg()
    image = tmp_path / "frame.jpg"
    Image.new("RGB", (640, 360), (16, 22, 30)).save(image, quality=95)
    audio = tmp_path / "narration.wav"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=2.0",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    return image, audio


def test_filter_path_escapes_windows_drive_and_apostrophes() -> None:
    value = _filter_path(Path(r"C:\Users\O'Brien\captions.ass"))
    assert value == r"C\:/Users/O\'Brien/captions.ass"


async def test_render_burns_in_captions_and_adds_soft_track(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg()
    image, audio = _fixtures(tmp_path)
    settings = preset_settings("clean_editorial")
    cues = [
        CaptionCue(index=1, start=0.1, end=1.9, text="Hello world नमस्ते"),
    ]
    ass_path = tmp_path / "captions.ass"
    ass_path.write_text(
        to_ass(
            cues,
            settings,
            aspect_ratio="16:9",
            width=640,
            height=360,
            font_family="Nirmala UI",
            language="hi",
        ),
        encoding="utf-8",
    )
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text(to_srt(cues, settings), encoding="utf-8")
    output = tmp_path / "out.mp4"

    renderer = FFmpegSceneRenderer(ffmpeg, fps=24, timeout_seconds=120)
    result = await renderer.render(
        [SceneClip(position=1, image_path=image, duration_seconds=2.0)],
        audio,
        srt_path,
        output,
        2.0,
        width=640,
        height=360,
        aspect_ratio="16:9",
        caption_render=CaptionRender(
            ass_path=ass_path,
            fonts_dir=default_fonts_dir(),
            soft_subtitle_path=srt_path,
            burn_in=True,
            soft_subs=True,
            cue_count=1,
            source="narration_alignment",
        ),
    )

    assert result.passed, [check.detail for check in result.checks if check.status == "failed"]
    assert result.path.is_file() and result.path.stat().st_size > 1024
    checks = {check.name: check for check in result.checks}
    assert checks["subtitle_stream"].status == "passed"
    assert result.caption["burn_in"] is True
    assert result.caption["cue_count"] == 1

    frame_path = tmp_path / "frame.png"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "1.0",
            "-i",
            str(output),
            "-frames:v",
            "1",
            str(frame_path),
        ],
        check=True,
        capture_output=True,
    )
    with Image.open(frame_path).convert("RGB") as frame:
        bottom = frame.crop((0, int(frame.height * 0.65), frame.width, frame.height))
        bright = sum(
            1
            for pixel in bottom.getdata()
            if 0.2126 * pixel[0] + 0.7152 * pixel[1] + 0.0722 * pixel[2] > 180
        )
    assert bright > 20, "burned-in caption pixels were not found in the rendered frame"


async def test_render_rejects_missing_caption_file(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg()
    image, audio = _fixtures(tmp_path)
    renderer = FFmpegSceneRenderer(ffmpeg, fps=24, timeout_seconds=60)
    with pytest.raises(FileNotFoundError, match="Caption subtitle file not found"):
        await renderer.render(
            [SceneClip(position=1, image_path=image, duration_seconds=2.0)],
            audio,
            tmp_path / "missing.srt",
            tmp_path / "out.mp4",
            2.0,
            width=640,
            height=360,
            aspect_ratio="16:9",
            caption_render=CaptionRender(
                ass_path=tmp_path / "missing.ass",
                burn_in=True,
                soft_subs=False,
            ),
        )
