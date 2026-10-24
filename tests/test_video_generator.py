from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.video.agent import VideoGeneratorAgent
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    ResearchBrief,
    Scene,
    ScenePlan,
    VideoRender,
)


class FakeVideoRenderer:
    name = "test-renderer"

    async def render(
        self,
        narration_path: Path,
        subtitle_path: Path,
        output_path: Path,
        duration_seconds: float,
    ) -> None:
        assert subtitle_path.is_file()
        assert duration_seconds == 2.5
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"test-video")


@pytest.mark.asyncio
async def test_video_generator_creates_draft_with_synced_subtitles_and_timeline(
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/video",
            canonical_url="https://bbc.example/video",
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
            hook="Here is what is known.",
            introduction="Independent sources corroborate the report.",
            main_story=["Officials said an earthquake struck the city."],
            context=[],
            closing="More information may follow.",
            full_text="A neutral narration.",
            estimated_duration_seconds=10,
            target_duration_seconds=60,
            writer_name="test",
            review_reasons=[],
        )
        session.add(script)
        await session.flush()
        plan = ScenePlan(
            script_id=script.id,
            status="draft",
            total_duration_seconds=10,
            planner_name="test",
        )
        session.add(plan)
        await session.flush()
        session.add(
            Scene(
                scene_plan_id=plan.id,
                position=1,
                start_seconds=0,
                duration_seconds=10,
                narration="Officials said an earthquake struck the city.",
                subtitle="Officials said an earthquake struck the city.",
                keywords=["earthquake", "city"],
                scene_description="Neutral map visual.",
                stock_footage_query="earthquake city neutral news footage",
                image_search_query="earthquake city editorial image",
                animation_suggestion="Slow zoom.",
                transition="fade",
            )
        )
        narration = NarrationAudio(
            script_id=script.id,
            scene_plan_id=plan.id,
            status="completed",
            provider_name="test",
            voice_name="test-voice",
            wav_path=str(tmp_path / "narration.wav"),
            mp3_path=None,
            duration_seconds=2.5,
            sample_rate=16_000,
        )
        session.add(narration)
        await session.flush()
        session.add_all(
            [
                NarrationChunk(
                    narration_audio_id=narration.id,
                    position=1,
                    text="Officials said an earthquake struck the city.",
                    wav_path=str(tmp_path / "chunk-001.wav"),
                    start_seconds=0.0,
                    duration_seconds=1.0,
                ),
                NarrationChunk(
                    narration_audio_id=narration.id,
                    position=2,
                    text="Emergency crews began their response.",
                    wav_path=str(tmp_path / "chunk-002.wav"),
                    start_seconds=1.0,
                    duration_seconds=1.5,
                ),
            ]
        )
        await session.commit()

    result = await VideoGeneratorAgent(
        Settings(database_url="sqlite+aiosqlite:///:memory:", video_output_dir=tmp_path / "video"),
        sessions,
        renderer=FakeVideoRenderer(),
    ).execute()

    assert result.status == "completed"
    assert result.items_created == 1
    async with sessions() as session:
        render = await session.scalar(select(VideoRender))

    assert render is not None
    assert render.status == "draft"
    assert Path(render.video_path).read_bytes() == b"test-video"
    assert "00:00:01,000 --> 00:00:02,500" in Path(render.subtitle_path).read_text()
    assert "stock_footage_query" in Path(render.timeline_path).read_text()
    await engine.dispose()
