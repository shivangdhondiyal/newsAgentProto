from __future__ import annotations

from backend.app.agents.video.subtitles import MAX_LINE_CHARACTERS, render_srt


class Chunk:
    def __init__(self, text: str, start: float, duration: float) -> None:
        self.text = text
        self.start_seconds = start
        self.duration_seconds = duration


def _cues(srt: str) -> list[tuple[str, str, str]]:
    blocks = [block for block in srt.strip().split("\n\n") if block.strip()]
    parsed: list[tuple[str, str, str]] = []
    for block in blocks:
        lines = block.splitlines()
        start, end = lines[1].split(" --> ")
        parsed.append((start, end, "\n".join(lines[2:])))
    return parsed


def test_long_narration_chunk_is_split_into_readable_cues() -> None:
    chunk = Chunk(
        "Here is what is known so far about Lula re-elected in Brazil as Bolsonaro "
        "rejects results. This report is based on information corroborated by 3 "
        "independent sources. Lula re-elected in Brazil as Bolsonaro rejects results "
        "Result keeps some distance between Latin America's biggest economy and "
        "Washington Brazil's President Luiz Inacio Lula da Silva, a vocal critic of "
        "U.S. influence in Latin America.",
        0.0,
        23.0,
    )

    cues = _cues(render_srt([chunk]))

    assert len(cues) > 4
    for _, _, text in cues:
        assert len(text.splitlines()) <= 2
        assert all(len(line) <= MAX_LINE_CHARACTERS + 4 for line in text.splitlines())


def test_abbreviations_do_not_create_one_word_cues() -> None:
    chunk = Chunk(
        "Washington criticised widening U.S. influence in Latin America.",
        0.0,
        6.0,
    )

    cues = _cues(render_srt([chunk]))

    assert all(len(text.strip(" .")) > 4 for _, _, text in cues)


def test_cues_stay_inside_measured_chunk_timing() -> None:
    chunk = Chunk("First sentence here. Second sentence follows it closely.", 4.0, 6.0)

    cues = _cues(render_srt([chunk]))

    assert cues[0][0] == "00:00:04,000"
    assert cues[-1][1] == "00:00:10,000"
