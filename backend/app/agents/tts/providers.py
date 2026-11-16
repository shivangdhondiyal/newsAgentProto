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


class KokoroSpeechSynthesizer:
    """Kokoro-82M local TTS via kokoro-onnx; no account, no API, 24 kHz mono WAV."""

    name = "kokoro"
    sample_rate = 24_000

    def __init__(
        self,
        model_path: Path,
        voices_path: Path,
        *,
        voice: str = "am_michael",
        speed: float = 1.05,
        auto_download: bool = False,
    ) -> None:
        self._model_path = model_path
        self._voices_path = voices_path
        self._voice = voice
        self._speed = max(0.5, min(2.0, float(speed)))
        self._auto_download = auto_download
        self._kokoro = None

    @property
    def voice_name(self) -> str:
        return self._voice

    @property
    def delivery(self) -> dict[str, float]:
        return {
            "speed": self._speed,
        }

    def _ensure_kokoro(self) -> None:
        if self._kokoro is not None:
            return
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise RuntimeError(
                "kokoro-onnx is not installed. Install it to use the kokoro TTS provider."
            ) from exc
        from backend.app.media.voice_catalog import ensure_kokoro_models

        try:
            model_path, voices_path = ensure_kokoro_models(
                self._model_path, self._voices_path, self._auto_download
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(str(exc)) from exc
        self._kokoro = Kokoro(str(model_path), str(voices_path))

    @staticmethod
    def _language_for(voice: str) -> str:
        return "en-gb" if voice.startswith("b") else "en-us"

    @staticmethod
    def _trim(samples, threshold: float = 0.008):
        import numpy as np

        idx = np.where(np.abs(samples) > threshold)[0]
        if not len(idx):
            return samples
        start = max(0, idx[0] - int(0.01 * KokoroSpeechSynthesizer.sample_rate))
        end = min(len(samples), idx[-1] + int(0.03 * KokoroSpeechSynthesizer.sample_rate))
        return samples[start:end]

    async def synthesize(self, text: str, output_path: Path) -> None:
        if not str(text or "").strip():
            raise ValueError("Cannot synthesize empty narration text")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        def _render() -> None:
            self._ensure_kokoro()
            from scipy.io import wavfile

            audio, _ = self._kokoro.create(
                text,
                voice=self._voice,
                speed=self._speed,
                lang=self._language_for(self._voice),
            )
            import numpy as np

            trimmed = self._trim(np.asarray(audio, dtype=np.float64))
            wavfile.write(
                str(output_path),
                self.sample_rate,
                (np.clip(trimmed, -1.0, 1.0) * 32767).astype("<i2"),
            )

        await asyncio.to_thread(_render)
        if not output_path.is_file():
            raise RuntimeError("Kokoro completed without producing a WAV file")
