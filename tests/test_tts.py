from __future__ import annotations

import wave
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.tts.agent import TTSAgent
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    ResearchBrief,
    ScenePlan,
)


class FakeSpeechSynthesizer:
    name = "test-synthesizer"
    voice_name = "test-voice"

    async def synthesize(self, text: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(b"\x10\x00" * 16_000)


@pytest.mark.asyncio
async def test_tts_creates_merged_audio_and_chunk_timing(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/audio",
            canonical_url="https://bbc.example/audio",
            title="Earthquake strikes city",
            normalized_text="Officials said an earthquake struck the city.",
        )
        session.add(article)
        await session.flush()
        brief = ResearchBrief(
            article_id=article.id,
            status="completed",
            headline=article.title,
            key_facts=[],
            context=[],
            uncertainties=[],
            source_count=2,
            summarizer_name="test",
        )
        session.add(brief)
        await session.flush()
        script = NewsScript(
            article_id=article.id,
            research_brief_id=brief.id,
            status="draft",
            title=article.title,
            hook="Here is what is known so far.",
            introduction="Independent reports corroborate the event.",
            main_story=["Officials said an earthquake struck the city."],
            context=["Emergency crews began their response."],
            closing="More verified information may follow.",
            full_text=(
                "Here is what is known so far. Independent reports corroborate the event. "
                "Officials said an earthquake struck the city. "
                "Emergency crews began their response. "
                "More verified information may follow."
            ),
            estimated_duration_seconds=30,
            target_duration_seconds=60,
            writer_name="test",
            review_reasons=[],
        )
        session.add(script)
        await session.flush()
        session.add(
            ScenePlan(
                script_id=script.id,
                status="draft",
                total_duration_seconds=30,
                planner_name="test",
            )
        )
        await session.commit()

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        tts_output_dir=tmp_path / "audio",
        tts_chunk_max_characters=80,
        tts_generate_mp3=False,
    )
    result = await TTSAgent(settings, sessions, FakeSpeechSynthesizer()).execute()

    assert result.status == "completed"
    assert result.items_created == 1
    async with sessions() as session:
        narration = await session.scalar(select(NarrationAudio))
        chunks = list(
            (await session.scalars(select(NarrationChunk).order_by(NarrationChunk.position))).all()
        )

    assert narration is not None
    assert Path(narration.wav_path).is_file()
    assert narration.duration_seconds == len(chunks)
    assert [chunk.start_seconds for chunk in chunks] == list(range(len(chunks)))
    assert all(Path(chunk.wav_path).is_file() for chunk in chunks)
    await engine.dispose()
