from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptionResult:
    segments: list[dict[str, Any]]
    provider: str
    detail: str
    warnings: tuple[str, ...] = ()


class TranscriptionUnavailable(RuntimeError):
    """No supported local speech-recognition backend is installed."""


def transcription_status() -> dict[str, Any]:
    """Report the optional ASR backends without importing heavy dependencies."""

    faster_whisper = importlib.util.find_spec("faster_whisper") is not None
    whisper = importlib.util.find_spec("whisper") is not None
    return {
        "available": faster_whisper or whisper,
        "faster_whisper": faster_whisper,
        "whisper": whisper,
        "fallback": "narration_alignment",
        "note": (
            "Install faster-whisper for word-level speech recognition. Without it, "
            "captions are aligned to the measured TTS chunk timing and are labelled "
            "as not speech-verified."
        ),
    }


def transcribe(
    audio_path: Path,
    *,
    provider: str = "auto",
    model_size: str = "base",
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe the final voiceover with an optional local ASR backend.

    The application deliberately does not pretend that script text is verified
    speech. If no backend is available this raises ``TranscriptionUnavailable``
    and the caption service records the narration-aligned fallback instead.
    """

    if not audio_path.is_file():
        raise FileNotFoundError(f"Narration audio not found: {audio_path}")
    choice = (provider or "auto").strip().lower()
    if choice in {"none", "off", "disabled"}:
        raise TranscriptionUnavailable("Speech recognition is disabled in settings.")
    if choice in {"auto", "faster_whisper", "faster-whisper"}:
        if importlib.util.find_spec("faster_whisper") is not None:
            return _transcribe_faster_whisper(audio_path, model_size=model_size, language=language)
        if choice != "auto":
            raise TranscriptionUnavailable(
                "faster-whisper is not installed. Install it or choose the "
                "narration-aligned fallback."
            )
    if choice in {"auto", "whisper"}:
        if importlib.util.find_spec("whisper") is not None:
            return _transcribe_whisper(audio_path, model_size=model_size, language=language)
        if choice != "auto":
            raise TranscriptionUnavailable(
                "openai-whisper is not installed. Install it or choose the "
                "narration-aligned fallback."
            )
    raise TranscriptionUnavailable(
        "No local speech-recognition backend is installed. Captions will use the "
        "measured narration timing and will be labelled as not speech-verified."
    )


def _transcribe_faster_whisper(
    audio_path: Path, *, model_size: str, language: str | None
) -> TranscriptionResult:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segments, info = model.transcribe(
        str(audio_path),
        language=None if not language or language == "auto" else language.split("-")[0],
        word_timestamps=True,
        vad_filter=True,
    )
    payload: list[dict[str, Any]] = []
    for segment in segments:
        words = [
            {
                "word": word.word,
                "start": float(word.start or segment.start),
                "end": float(word.end or segment.end),
            }
            for word in (getattr(segment, "words", None) or [])
        ]
        payload.append(
            {
                "text": segment.text,
                "start": float(segment.start),
                "end": float(segment.end),
                "words": words,
            }
        )
    detected = getattr(info, "language", None) or language or "en"
    return TranscriptionResult(
        segments=payload,
        provider="faster-whisper",
        detail=f"faster-whisper ({model_size}, language {detected})",
    )


def _transcribe_whisper(
    audio_path: Path, *, model_size: str, language: str | None
) -> TranscriptionResult:
    import whisper  # type: ignore[import-not-found]

    model = whisper.load_model(model_size)
    result = model.transcribe(
        str(audio_path),
        language=None if not language or language == "auto" else language.split("-")[0],
        word_timestamps=True,
        verbose=False,
    )
    payload: list[dict[str, Any]] = []
    for segment in result.get("segments") or []:
        payload.append(
            {
                "text": segment.get("text", ""),
                "start": float(segment.get("start", 0.0) or 0.0),
                "end": float(segment.get("end", 0.0) or 0.0),
                "words": [
                    {
                        "word": word.get("word", ""),
                        "start": float(word.get("start", 0.0) or 0.0),
                        "end": float(word.get("end", 0.0) or 0.0),
                    }
                    for word in (segment.get("words") or [])
                ],
            }
        )
    return TranscriptionResult(
        segments=payload,
        provider="whisper",
        detail=f"openai-whisper ({model_size})",
    )
