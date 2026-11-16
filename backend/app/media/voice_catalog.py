"""Local voice catalog shared by the TTS providers and the dashboard.

Kokoro-82M (Apache-2.0) is the free local voice used by the faceless-shorts
skill: no account, no API, deterministic 24 kHz mono output. This module keeps
the recommended voice list, the model file locations, and the availability
checks in one place so the API, the preview endpoint, and the TTS agent all
describe the same reality.
"""

from __future__ import annotations

import importlib.util
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KOKORO_SAMPLE_RATE = 24_000
KOKORO_MODEL_FILENAME = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILENAME = "voices-v1.0.bin"
KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
    "kokoro-v1.0.onnx"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
    "voices-v1.0.bin"
)


@dataclass(frozen=True)
class KokoroVoice:
    key: str
    language: str
    description: str

    @property
    def name(self) -> str:
        label = self.key.split("_", 1)[-1].replace("_", " ").title()
        return f"Kokoro · {label} ({self.description})"


# The voices the skill recommends for narration; the full voices bin contains
# more, but these are the ones with known delivery characteristics.
KOKORO_VOICES: dict[str, KokoroVoice] = {
    voice.key: voice
    for voice in (
        KokoroVoice("am_michael", "en-us", "US male, steady explainer"),
        KokoroVoice("am_puck", "en-us", "US male, lively, playful"),
        KokoroVoice("am_fenrir", "en-us", "US male, deeper"),
        KokoroVoice("am_onyx", "en-us", "US male, low"),
        KokoroVoice("af_heart", "en-us", "US female, warm"),
        KokoroVoice("af_bella", "en-us", "US female, bright"),
        KokoroVoice("af_nicole", "en-us", "US female, soft and close"),
        KokoroVoice("bm_george", "en-gb", "UK male, documentary"),
        KokoroVoice("bm_fable", "en-gb", "UK male, storyteller"),
        KokoroVoice("bf_emma", "en-gb", "UK female, calm"),
    )
}

_DOWNLOAD_LOCK = threading.Lock()


def kokoro_language(voice_key: str) -> str:
    """British voices start with ``b`` in Kokoro's naming scheme."""

    return "en-gb" if voice_key.startswith("b") else "en-us"


def kokoro_package_available() -> bool:
    return (
        importlib.util.find_spec("kokoro_onnx") is not None
        and importlib.util.find_spec("onnxruntime") is not None
    )


def kokoro_paths(settings: Any) -> tuple[Path, Path]:
    return (
        Path(settings.tts_kokoro_model_path),
        Path(settings.tts_kokoro_voices_path),
    )


def _usable(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 1_000_000


def kokoro_models_present(settings: Any) -> bool:
    model_path, voices_path = kokoro_paths(settings)
    return _usable(model_path) and _usable(voices_path)


def kokoro_ready(settings: Any) -> bool:
    """Ready means synthesis can start now, without a surprise download."""

    return kokoro_package_available() and kokoro_models_present(settings)


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "NewsAutomationPlatform/0.2"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS URL
        with temporary.open("wb") as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
    if not _usable(temporary):
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded file is empty or incomplete: {destination.name}")
    temporary.replace(destination)


def ensure_kokoro_models(
    model_path: Path, voices_path: Path, auto_download: bool = False
) -> tuple[Path, Path]:
    """Return the model paths, downloading them once when that is allowed."""

    with _DOWNLOAD_LOCK:
        for path, url in (
            (model_path, KOKORO_MODEL_URL),
            (voices_path, KOKORO_VOICES_URL),
        ):
            if _usable(path):
                continue
            if not auto_download:
                raise FileNotFoundError(
                    f"Kokoro model file not found: {path}. Run the Kokoro download "
                    "once, or set NEWS_TTS_KOKORO_AUTO_DOWNLOAD=true."
                )
            _download(url, path)
    return model_path, voices_path


def installed_kokoro_voices(settings: Any) -> list[KokoroVoice]:
    if not kokoro_package_available():
        return []
    return list(KOKORO_VOICES.values())
