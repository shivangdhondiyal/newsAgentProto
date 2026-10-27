from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from backend.app.core.config import get_settings
from backend.app.media.renderer import FFmpegSceneRenderer, SceneClip

BASE = Path("assets/generated/video/6b51e840-5cd5-44a6-8d32-1bd21e628272")
AUDIO = Path(
    "assets/generated/audio/mastered/78601646-76b1-4558-818c-7e338bd7b55a/narration-mastered.wav"
)
DURATIONS = [4.860, 5.335, 5.139, 4.499, 5.383, 5.242]


async def run(aspect: str, width: int, height: int, round_number: int) -> None:
    settings = get_settings()
    renderer = FFmpegSceneRenderer(
        settings.video_ffmpeg_executable,
        fps=settings.video_fps,
        timeout_seconds=settings.video_render_timeout_seconds,
        scratch_dir=settings.video_scratch_dir,
    )
    clips = [
        SceneClip(
            position=index + 1,
            image_path=BASE / "frames" / aspect.replace(":", "x") / f"scene-{index + 1:03d}.jpg",
            duration_seconds=duration,
            transition="smoothleft" if index in (3, 5) else "fade",
            motion="static",
        )
        for index, duration in enumerate(DURATIONS)
    ]
    output = Path(f"assets/generated/video/_bench-{aspect.replace(':', 'x')}-{round_number}.mp4")
    started = time.perf_counter()
    result = await renderer.render(
        clips,
        AUDIO,
        BASE / "subtitles.srt",
        output,
        sum(DURATIONS),
        width=width,
        height=height,
        aspect_ratio=aspect,
    )
    elapsed = time.perf_counter() - started
    print(
        f"{aspect} round {round_number}: {elapsed:6.1f}s  {result.width}x{result.height} "
        f"duration={result.duration_seconds:.3f}s size={result.size_bytes} "
        f"passed={result.passed}"
    )
    for check in result.checks:
        if check.status != "passed":
            print(f"   {check.name}: {check.status} {check.detail}")


async def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    for round_number in range(1, rounds + 1):
        await run("16:9", 1920, 1080, round_number)
        await run("9:16", 1080, 1920, round_number)


asyncio.run(main())
