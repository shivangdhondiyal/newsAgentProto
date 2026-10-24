from __future__ import annotations

from pathlib import Path
from typing import Protocol


class VideoRenderer(Protocol):
    name: str

    async def render(
        self,
        narration_path: Path,
        subtitle_path: Path,
        output_path: Path,
        duration_seconds: float,
    ) -> None: ...
