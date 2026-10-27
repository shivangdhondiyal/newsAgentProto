from __future__ import annotations

import asyncio
from pathlib import Path


class PiperSpeechSynthesizer:
    """Piper CLI adapter; all synthesis runs locally and no text leaves the machine."""

    name = "piper"

    def __init__(
        self,
        executable: str,
        voice_model: Path,
        *,
        length_scale: float = 1.0,
        noise_scale: float = 0.667,
        noise_w: float = 0.8,
        sentence_silence: float = 0.2,
    ) -> None:
        self._executable = executable
        self._voice_model = voice_model
        self._length_scale = max(0.5, min(2.0, float(length_scale)))
        self._noise_scale = max(0.0, min(2.0, float(noise_scale)))
        self._noise_w = max(0.0, min(2.0, float(noise_w)))
        self._sentence_silence = max(0.0, min(3.0, float(sentence_silence)))

    @property
    def voice_name(self) -> str:
        return self._voice_model.stem

    @property
    def delivery(self) -> dict[str, float]:
        return {
            "length_scale": self._length_scale,
            "noise_scale": self._noise_scale,
            "noise_w": self._noise_w,
            "sentence_silence": self._sentence_silence,
        }

    async def synthesize(self, text: str, output_path: Path) -> None:
        if not self._voice_model.is_file():
            raise FileNotFoundError(f"Piper voice model not found: {self._voice_model}")
        if not str(text or "").strip():
            raise ValueError("Cannot synthesize empty narration text")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            self._executable,
            "--model",
            str(self._voice_model),
            "--output_file",
            str(output_path),
            "--length_scale",
            f"{self._length_scale:.4f}",
            "--noise_scale",
            f"{self._noise_scale:.4f}",
            "--noise_w",
            f"{self._noise_w:.4f}",
            "--sentence_silence",
            f"{self._sentence_silence:.4f}",
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
