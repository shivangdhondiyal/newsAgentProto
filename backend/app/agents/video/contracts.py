from __future__ import annotations

from pathlib import Path
from typing import Protocol

from backend.app.media.renderer import CaptionRender, RenderResult, SceneClip


class VideoRenderer(Protocol):
    name: str

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
    ) -> RenderResult: ...
