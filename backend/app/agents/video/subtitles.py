from __future__ import annotations

import re

from backend.app.db.models import NarrationChunk

# Broadcast caption convention: at most two lines of roughly 40 characters.
MAX_LINE_CHARACTERS = 40
MAX_CUE_CHARACTERS = 80
MIN_CUE_SECONDS = 1.2


def render_srt(chunks: list[NarrationChunk]) -> str:
    """Create a UTF-8 SRT track from measured chunk timings.

    A narration chunk is a synthesis unit, not a caption unit: a single chunk
    can run for twenty seconds. Each chunk is therefore split into readable
    cues, with the measured chunk duration shared out by character count so the
    captions stay locked to the audio.
    """

    entries: list[str] = []
    for chunk in chunks:
        text = " ".join((chunk.text or "").split())
        if not text:
            continue
        units = _caption_units(text)
        total_characters = sum(len(unit) for unit in units) or 1
        elapsed = 0.0
        for index, unit in enumerate(units):
            share = chunk.duration_seconds * (len(unit) / total_characters)
            start = chunk.start_seconds + elapsed
            end = chunk.start_seconds + chunk.duration_seconds
            if index < len(units) - 1:
                end = min(end, start + max(share, MIN_CUE_SECONDS))
            elapsed = end - chunk.start_seconds
            entries.append(
                f"{len(entries) + 1}\n"
                f"{_timestamp(start)} --> {_timestamp(max(end, start + 0.4))}\n"
                f"{_two_lines(unit)}"
            )
    return "\n\n".join(entries) + ("\n" if entries else "")


def _caption_units(text: str, max_characters: int = MAX_CUE_CHARACTERS) -> list[str]:
    units: list[str] = []
    for sentence in _sentences(text):
        if len(sentence) <= max_characters:
            units.append(sentence)
            continue
        units.extend(_split_clauses(sentence, max_characters))
    return _merge_short_units(units, max_characters)


# Periods that end an abbreviation rather than a sentence.
ABBREVIATION = re.compile(
    r"\b(?:U\.S|U\.K|E\.U|U\.N|Mr|Mrs|Ms|Dr|Sen|Rep|Gov|Gen|Lt|Col|Sgt|Jr|Sr|"
    r"vs|etc|Inc|Ltd|Co|No|St|a\.m|p\.m)\.",
    re.IGNORECASE,
)


def _sentences(text: str) -> list[str]:
    guarded = ABBREVIATION.sub(lambda match: match.group(0).replace(".", "\u2024"), text)
    parts = re.split(r"(?<=[.!?])\s+", guarded)
    return [part.replace("\u2024", ".").strip() for part in parts if part.strip()]


def _merge_short_units(units: list[str], max_characters: int) -> list[str]:
    """Fold a dangling fragment such as "results." into the cue before it."""

    merged: list[str] = []
    for unit in units:
        if (
            merged
            and len(unit) < 16
            and len(merged[-1]) + 1 + len(unit) <= max_characters
        ):
            merged[-1] = f"{merged[-1]} {unit}"
        else:
            merged.append(unit)
    return merged


def _split_clauses(sentence: str, max_characters: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for clause in re.split(r"(?<=[,;:])\s+", sentence):
        candidate = f"{current} {clause}".strip()
        if current and len(candidate) > max_characters:
            pieces.append(current)
            current = clause
        else:
            current = candidate
    if current:
        pieces.append(current)

    wrapped: list[str] = []
    for piece in pieces:
        if len(piece) <= max_characters:
            wrapped.append(piece)
            continue
        words: list[str] = []
        length = 0
        for word in piece.split():
            addition = len(word) + (1 if words else 0)
            if words and length + addition > max_characters:
                wrapped.append(" ".join(words))
                words, length = [word], len(word)
            else:
                words.append(word)
                length += addition
        if words:
            wrapped.append(" ".join(words))
    return wrapped


def _two_lines(text: str) -> str:
    """Break one cue into two balanced display lines."""

    if len(text) <= MAX_LINE_CHARACTERS:
        return text
    words = text.split()
    best = len(words)
    best_score: tuple[int, int] | None = None
    for split in range(1, len(words)):
        first = " ".join(words[:split])
        second = " ".join(words[split:])
        # Prefer two lines that both fit; otherwise minimise the overflow.
        overflow = max(0, len(first) - MAX_LINE_CHARACTERS) + max(
            0, len(second) - MAX_LINE_CHARACTERS
        )
        score = (overflow, abs(len(first) - len(second)))
        if best_score is None or score < best_score:
            best, best_score = split, score
    return " ".join(words[:best]) + "\n" + " ".join(words[best:])


def _timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1_000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"
