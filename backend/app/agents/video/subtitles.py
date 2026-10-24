from __future__ import annotations

from backend.app.db.models import NarrationChunk


def render_srt(chunks: list[NarrationChunk]) -> str:
    """Create a UTF-8 SRT track from synthesized chunk timings, not guessed script timings."""

    entries: list[str] = []
    for position, chunk in enumerate(chunks, start=1):
        start = _timestamp(chunk.start_seconds)
        end = _timestamp(chunk.start_seconds + chunk.duration_seconds)
        text = " ".join(chunk.text.split())
        entries.append(f"{position}\n{start} --> {end}\n{text}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def _timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1_000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"
