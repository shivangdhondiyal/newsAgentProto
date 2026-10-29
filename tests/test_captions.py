from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    CaptionTrack,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    ResearchBrief,
    ScenePlan,
)
from backend.app.media.captions import (
    CAPTION_PRESETS,
    CaptionCue,
    allocate_words,
    default_fonts_dir,
    generate_from_chunks,
    list_caption_presets,
    parse_srt,
    parse_vtt,
    preset_settings,
    resolve_font_family,
    to_ass,
    to_srt,
    to_vtt,
    validate_cues,
    validate_settings,
    wrap_caption_text,
)
from backend.app.services.captions import (
    CaptionError,
    CaptionRegenerationRequired,
    CaptionService,
)


class Chunk:
    def __init__(self, text: str, start: float, duration: float) -> None:
        self.text = text
        self.start_seconds = start
        self.duration_seconds = duration


def test_curated_caption_presets_are_complete() -> None:
    presets = list_caption_presets()
    assert len(presets) == 6
    assert {preset["key"] for preset in presets} == {
        "clean_editorial",
        "professional_news",
        "documentary",
        "social_media",
        "kinetic",
        "emphasis",
    }
    assert CAPTION_PRESETS["clean_editorial"].settings.preset_key == "clean_editorial"
    for preset in presets:
        assert preset["name"]
        assert preset["description"]
        assert preset["settings"]["landscape"]["max_lines"] <= 4
        assert preset["settings"]["portrait"]["safe_bottom_ratio"] >= 0.12


def test_long_chunk_is_split_into_readable_cues_without_dropping_words() -> None:
    text = (
        "Here is what is known so far about Lula re-elected in Brazil as Bolsonaro "
        "rejects results. This report is based on information corroborated by 3 "
        "independent sources."
    )
    generation = generate_from_chunks(
        [Chunk(text, 0.0, 9.0)],
        preset_settings("clean_editorial"),
        duration=9.0,
    )
    assert generation.cues
    assert generation.source == "narration_alignment"
    assert " ".join(cue.text for cue in generation.cues).split() == text.split()
    layout = preset_settings("clean_editorial").layout_for("16:9")
    for cue in generation.cues:
        lines = wrap_caption_text(cue.text, layout.max_chars_per_line, layout.max_lines)
        assert len(lines) <= layout.max_lines
        assert all(len(line) <= layout.max_chars_per_line + 2 for line in lines)
        assert cue.end > cue.start
    ordered = sorted(generation.cues, key=lambda cue: cue.start)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        assert current.start >= previous.end - 0.001


def test_srt_and_vtt_round_trip_preserve_text_and_timing() -> None:
    settings = preset_settings("professional_news")
    cues = [
        CaptionCue(
            index=1,
            start=0.5,
            end=2.25,
            text="The finance minister said GDP grew 7.2%.",
            words=allocate_words(
                "The finance minister said GDP grew 7.2%.", 0.5, 1.75, emphasis=True
            ),
            speaker="Anchor",
        ),
        CaptionCue(index=2, start=2.4, end=4.0, text="नई दिल्ली में बारिश हुई।"),
    ]
    srt = to_srt(cues, settings)
    vtt = to_vtt(cues, settings, source="narration_alignment")
    srt_cues = parse_srt(srt)
    vtt_cues = parse_vtt(vtt)
    assert [cue.text for cue in srt_cues] == [cue.text for cue in cues]
    assert [cue.text for cue in vtt_cues] == [cue.text for cue in cues]
    assert abs(srt_cues[0].start - 0.5) < 0.001
    assert abs(srt_cues[0].end - 2.25) < 0.001


def test_ass_escapes_reserved_characters_and_resolves_devanagari_font() -> None:
    settings = preset_settings("documentary")
    cues = [
        CaptionCue(
            index=1,
            start=0.0,
            end=2.0,
            text="Budget {draft} C:\\temp 100%",
        )
    ]
    ass = to_ass(
        cues,
        settings,
        aspect_ratio="16:9",
        width=1920,
        height=1080,
        font_family="Nirmala UI",
        language="hi",
    )
    assert "Nirmala UI" in ass
    assert r"\{draft\}" in ass
    assert r"C:\\temp" in ass
    assert "PlayResX: 1920" in ass
    family, warnings = resolve_font_family("Segoe UI", language="hi")
    if default_fonts_dir() is not None:
        assert family in {"Nirmala UI", "Noto Sans Devanagari", "Mangal", "Segoe UI"}
        assert any("Devanagari" in warning for warning in warnings) or family == "Nirmala UI"


def test_validation_reports_empty_overlap_and_out_of_range_cues() -> None:
    settings = preset_settings("clean_editorial")
    cues = [
        CaptionCue(index=1, start=0.0, end=3.0, text="First cue."),
        CaptionCue(index=2, start=2.5, end=4.0, text="Overlapping cue."),
        CaptionCue(index=3, start=5.0, end=9.0, text="   "),
        CaptionCue(index=4, start=10.0, end=14.0, text="Past the narration."),
    ]
    checks = {
        check.name: check
        for check in validate_cues(
            cues,
            duration=12.0,
            settings=settings,
            aspect_ratio="16:9",
            language="en",
            font_family="Segoe UI",
        )
    }
    assert checks["empty_cues"].status == "failed"
    assert checks["overlap"].status == "failed"
    assert checks["within_media"].status == "failed"
    assert checks["cues_present"].status == "passed"


def test_settings_validation_rejects_unreadable_frame_geometry() -> None:
    settings = preset_settings("social_media").with_updates(
        {"landscape": {"font_size": 120, "max_chars_per_line": 80}}
    )
    checks = {
        check.name: check
        for check in validate_settings(settings, width=1920, height=1080, aspect_ratio="16:9")
    }
    assert checks["frame_width"].status == "failed"


async def _seed(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = Article(
            source_id="test",
            source_name="Test Wire",
            url="https://example.test/story",
            canonical_url="https://example.test/story",
            title="Test story",
            normalized_text="Test story text.",
        )
        session.add(article)
        await session.flush()
        brief = ResearchBrief(
            article_id=article.id,
            status="completed",
            headline="Test story",
            key_facts=[],
            context=[],
            uncertainties=[],
            source_count=3,
            summarizer_name="test",
        )
        session.add(brief)
        await session.flush()
        script = NewsScript(
            article_id=article.id,
            research_brief_id=brief.id,
            status="draft",
            title="Test story",
            hook="",
            introduction="",
            main_story=[],
            context=[],
            closing="",
            full_text="Hello world. This is a test of the caption system.",
            estimated_duration_seconds=6,
            target_duration_seconds=60,
            writer_name="test",
            review_reasons=[],
        )
        session.add(script)
        await session.flush()
        plan = ScenePlan(
            script_id=script.id,
            status="draft",
            total_duration_seconds=6,
            planner_name="test",
        )
        session.add(plan)
        await session.flush()
        wav = tmp_path / "narration.wav"
        wav.write_bytes(b"RIFF")
        narration = NarrationAudio(
            script_id=script.id,
            scene_plan_id=plan.id,
            status="completed",
            provider_name="test",
            voice_name="test",
            wav_path=str(wav),
            mp3_path=None,
            duration_seconds=6,
            sample_rate=16000,
        )
        session.add(narration)
        await session.flush()
        session.add_all(
            [
                NarrationChunk(
                    narration_audio_id=narration.id,
                    position=1,
                    text="Hello world.",
                    wav_path=str(wav),
                    start_seconds=0,
                    duration_seconds=2,
                ),
                NarrationChunk(
                    narration_audio_id=narration.id,
                    position=2,
                    text="This is a test of the caption system.",
                    wav_path=str(wav),
                    start_seconds=2,
                    duration_seconds=4,
                ),
            ]
        )
        await session.commit()
        return engine, sessions, script.id, narration.id


async def test_service_generates_persists_and_exports(tmp_path: Path) -> None:
    engine, sessions, script_id, narration_id = await _seed(tmp_path)
    settings = Settings(
        caption_output_dir=tmp_path / "captions",
        caption_transcription_provider="none",
    )
    service = CaptionService(settings, sessions)
    async with sessions() as session:
        track = await service.generate_for_script(session, script_id)
        assert track.status == "ready"
        assert track.source == "narration_alignment"
        assert track.narration_audio_id == narration_id
        assert track.cues_json
        assert track.srt_path and Path(track.srt_path).is_file()
        assert track.vtt_path and Path(track.vtt_path).is_file()
        assert set(track.ass_paths) == {"16:9", "9:16"}
        assert all(Path(path).is_file() for path in track.ass_paths.values())
        assert any(check["status"] == "passed" for check in track.checks_json)
        payload = service.serialize_track(track)
        assert payload["source_label"].startswith("Narration-aligned")
        assert payload["srt_url"].endswith("/export/srt")
    await engine.dispose()


async def test_manual_edits_require_confirmation_before_regeneration(tmp_path: Path) -> None:
    engine, sessions, script_id, _ = await _seed(tmp_path)
    settings = Settings(
        caption_output_dir=tmp_path / "captions",
        caption_transcription_provider="none",
    )
    service = CaptionService(settings, sessions)
    async with sessions() as session:
        track = await service.generate_for_script(session, script_id)
        cues = [CaptionCue.from_dict(item) for item in track.cues_json]
        cues[0].text = "Edited opening line."
        edited = await service.update_cues(session, track.id, cues)
        assert edited.status == "edited"
        assert edited.edited is True
        assert edited.cues_json[0]["text"] == "Edited opening line."
        with pytest.raises(CaptionRegenerationRequired):
            await service.regenerate(session, track.id)
        regenerated = await service.regenerate(session, track.id, force=True)
        assert regenerated.edited is False
        assert regenerated.status == "ready"
        assert regenerated.cues_json[0]["text"] != "Edited opening line."
    await engine.dispose()


async def test_import_replaces_cues_and_marks_track_imported(tmp_path: Path) -> None:
    engine, sessions, script_id, _ = await _seed(tmp_path)
    settings = Settings(
        caption_output_dir=tmp_path / "captions",
        caption_transcription_provider="none",
    )
    service = CaptionService(settings, sessions)
    content = (
        "1\n00:00:00,200 --> 00:00:01,400\nImported first line.\n\n"
        "2\n00:00:01,600 --> 00:00:03,000\nImported second line.\n"
    )
    async with sessions() as session:
        track = await service.generate_for_script(session, script_id)
        imported = await service.import_subtitles(session, track.id, content=content, format="srt")
        assert imported.source == "imported"
        assert [cue["text"] for cue in imported.cues_json] == [
            "Imported first line.",
            "Imported second line.",
        ]
        with pytest.raises(CaptionError):
            await service.import_subtitles(
                session, track.id, content="not a subtitle", format="srt"
            )
    await engine.dispose()


async def test_narration_change_marks_existing_track_stale(tmp_path: Path) -> None:
    engine, sessions, script_id, narration_id = await _seed(tmp_path)
    settings = Settings(
        caption_output_dir=tmp_path / "captions",
        caption_transcription_provider="none",
    )
    service = CaptionService(settings, sessions)
    async with sessions() as session:
        track = await service.generate_for_script(session, script_id)
        updated = await service.mark_stale_for_narration(
            session, narration_id, "Narration changed."
        )
        assert updated is not None
        assert updated.stale is True
        assert updated.stale_reason == "Narration changed."
        stored = await session.scalar(select(CaptionTrack).where(CaptionTrack.id == track.id))
        assert stored is not None and stored.stale is True
    await engine.dispose()
