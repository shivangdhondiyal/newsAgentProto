from __future__ import annotations

from dataclasses import dataclass

from backend.app.media.timeline import align_scenes_to_narration


@dataclass
class FakeChunk:
    text: str
    start_seconds: float
    duration_seconds: float


@dataclass
class FakeScene:
    position: int
    narration: str


def test_scene_timing_follows_measured_narration_chunks() -> None:
    chunks = [
        FakeChunk("The first sentence.", 0.0, 1.5),
        FakeChunk("The second sentence.", 1.7, 2.0),
        FakeChunk("The final sentence.", 4.1, 1.2),
    ]
    scenes = [
        FakeScene(1, "The first sentence."),
        FakeScene(2, "The second sentence."),
        FakeScene(3, "The final sentence."),
    ]
    timings = align_scenes_to_narration(scenes, chunks, 5.3)

    assert [timing.position for timing in timings] == [1, 2, 3]
    assert timings[0].start_seconds == 0.0
    # A scene holds through the pause before the next sentence begins.
    assert timings[0].end_seconds == 1.7
    assert timings[1].start_seconds == 1.7
    assert timings[1].end_seconds == 4.1
    assert timings[2].start_seconds == 4.1
    assert timings[2].end_seconds == 5.3
    assert abs(sum(t.duration_seconds for t in timings) - 5.3) < 0.01


def test_unmatched_scene_gets_proportional_remaining_time() -> None:
    chunks = [
        FakeChunk("The first sentence.", 0.0, 2.0),
        FakeChunk("The second sentence.", 2.2, 2.0),
    ]
    scenes = [
        FakeScene(1, "The first sentence."),
        FakeScene(2, "Text that was edited after synthesis."),
    ]
    timings = align_scenes_to_narration(scenes, chunks, 4.2)
    assert timings[1].start_seconds == 2.0
    assert timings[1].end_seconds == 4.2
