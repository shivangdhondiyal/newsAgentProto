from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


class TimedText(Protocol):
    text: str
    start_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class SceneTiming:
    """Narration-aligned timing for one scene, not the planner's rough estimate."""

    position: int
    start_seconds: float
    end_seconds: float
    narration: str

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


def align_scenes_to_narration(
    scenes: Iterable[object],
    chunks: list[TimedText],
    total_duration: float,
) -> list[SceneTiming]:
    """Map each scene's narration onto real synthesized audio timing.

    TTS chunks carry measured start/duration values. We build a character-to-time
    index over the joined chunk text, then locate each scene's narration in it.
    If a scene text cannot be located (edited script, punctuation drift), the
    remaining time is allocated proportionally by character count.
    """

    if not chunks:
        raise ValueError("Narration chunks are required to align scenes")

    joined_parts: list[str] = []
    spans: list[tuple[int, int, float, float]] = []
    cursor = 0
    for chunk in chunks:
        text = _normalize(chunk.text)
        if not text:
            continue
        if joined_parts:
            joined_parts.append(" ")
            cursor += 1
        start_char = cursor
        joined_parts.append(text)
        cursor += len(text)
        spans.append(
            (
                start_char,
                cursor,
                float(chunk.start_seconds),
                float(chunk.duration_seconds),
            )
        )
    joined = "".join(joined_parts)
    joined_lower = joined.lower()

    def time_at(char_index: int) -> float:
        char_index = max(0, min(len(joined), char_index))
        for start_char, end_char, start_time, duration in spans:
            if char_index <= end_char:
                span_length = max(1, end_char - start_char)
                ratio = (char_index - start_char) / span_length
                return start_time + duration * max(0.0, min(1.0, ratio))
        if spans:
            _, _, start_time, duration = spans[-1]
            return start_time + duration
        return 0.0

    scene_list = list(scenes)
    timings: list[SceneTiming] = []
    search_from = 0
    for index, scene in enumerate(scene_list):
        narration = _normalize(getattr(scene, "narration", "") or "")
        start: float | None = None
        end: float | None = None
        if narration:
            found = joined_lower.find(narration.lower(), search_from)
            if found < 0:
                found = joined_lower.find(narration.lower())
            if found >= 0:
                start = time_at(found)
                end = time_at(found + len(narration))
                search_from = found + len(narration)
        if start is None or end is None or end <= start:
            start = None
            end = None
        timings.append(
            SceneTiming(
                position=int(getattr(scene, "position", index + 1)),
                start_seconds=start or 0.0,
                end_seconds=end or 0.0,
                narration=narration,
            )
        )

    # Fill any scenes that could not be matched by splitting the unclaimed
    # timeline proportionally to their narration length.
    missing_indexes = [
        index for index, timing in enumerate(timings) if timing.end_seconds <= timing.start_seconds
    ]
    if missing_indexes:
        total_chars = sum(max(1, len(timings[index].narration)) for index in missing_indexes) or 1
        cursor_time = 0.0
        for timing in timings:
            if timing.end_seconds > timing.start_seconds:
                cursor_time = max(cursor_time, timing.end_seconds)
        remaining = max(0.0, total_duration - cursor_time)
        for index in missing_indexes:
            timing = timings[index]
            share = remaining * max(1, len(timing.narration)) / total_chars
            start = cursor_time
            cursor_time += share
            timings[index] = SceneTiming(
                position=timing.position,
                start_seconds=start,
                end_seconds=cursor_time,
                narration=timing.narration,
            )

    timings.sort(key=lambda item: item.position)
    if timings:
        first = timings[0]
        timings[0] = SceneTiming(
            position=first.position,
            start_seconds=0.0,
            end_seconds=max(first.end_seconds, 0.5),
            narration=first.narration,
        )
        # A pause between sentences belongs to the scene that just played, so a
        # scene extends to the next scene's start. Without this, the video would
        # compress every scene to absorb the pauses and drift out of sync.
        for index in range(len(timings) - 1):
            current = timings[index]
            following = timings[index + 1]
            if following.start_seconds > current.start_seconds:
                timings[index] = SceneTiming(
                    position=current.position,
                    start_seconds=current.start_seconds,
                    end_seconds=following.start_seconds,
                    narration=current.narration,
                )
        last = timings[-1]
        target_end = total_duration if total_duration > 0 else last.end_seconds
        if last.end_seconds < target_end:
            timings[-1] = SceneTiming(
                position=last.position,
                start_seconds=last.start_seconds,
                end_seconds=target_end,
                narration=last.narration,
            )
    return timings
