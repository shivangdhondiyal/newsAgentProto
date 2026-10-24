from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SpeechSynthesizer(Protocol):
    name: str
    voice_name: str

    async def synthesize(self, text: str, output_path: Path) -> None: ...
