from __future__ import annotations

import asyncio
from pathlib import Path


class PiperSpeechSynthesizer:
    """Piper CLI adapter; all synthesis runs locally and no text leaves the machine."""

    name = "piper"

    def __init__(self, executable: str, voice_model: Path) -> None:
        self._executable = executable
        self._voice_model = voice_model

    @property
    def voice_name(self) -> str:
        return self._voice_model.stem

    async def synthesize(self, text: str, output_path: Path) -> None:
        if not self._voice_model.is_file():
            raise FileNotFoundError(f"Piper voice model not found: {self._voice_model}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            self._executable,
            "--model",
            str(self._voice_model),
            "--output_file",
            str(output_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(text.encode("utf-8"))
        if process.returncode != 0:
            raise RuntimeError(
                f"Piper synthesis failed: {stderr.decode('utf-8', errors='replace')}"
            )
        if not output_path.is_file():
            raise RuntimeError("Piper completed without producing a WAV file")
