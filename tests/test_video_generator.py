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
    VideoVariant,
)
from backend.app.media.renderer import RenderCheck, RenderResult, SceneClip


class FakeVideoRenderer:
    """Record the new scene contract without invoking FFmpeg in unit tests."""

    name = "test-renderer"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def render(
        self,
        clips: list[SceneClip],
        narration_path: Path,
        subtitle_path: Path,
        output_path: Path,
        duration_seconds: float,
        *,
        width: int,
        height: int,
        transition_seconds: float = 0.4,
        aspect_ratio: str = "16:9",
    ) -> RenderResult:
        assert clips and all(clip.image_path.is_file() for clip in clips)
        assert subtitle_path.is_file()
        assert narration_path.is_file()
        assert duration_seconds == 2.5
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"test-video")
        self.calls.append(
            {
                "aspect_ratio": aspect_ratio,
                "width": width,
                "height": height,
                "clips": len(clips),
            }
        )
        return RenderResult(
            path=output_path,
            width=width,
            height=height,
            duration_seconds=duration_seconds,
            size_bytes=output_path.stat().st_size,
            checks=[
                RenderCheck(
                    name="frame_size",
                    status="passed",
                    detail=f"{width}x{height}",
                )
            ],
        )


@pytest.mark.asyncio
async def test_video_generator_creates_dual_aspect_drafts(
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    narration_path = tmp_path / "narration.wav"
    narration_path.write_bytes(b"RIFF-test-audio")
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
            wav_path=str(narration_path),
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

    renderer = FakeVideoRenderer()
    result = await VideoGeneratorAgent(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            video_output_dir=tmp_path / "video",
        ),
        sessions,
        renderer=renderer,
    ).execute()

    assert result.status == "completed"
    assert result.items_created == 1
    assert {call["aspect_ratio"] for call in renderer.calls} == {"16:9", "9:16"}
    async with sessions() as session:
        render = await session.scalar(select(VideoRender))
        variants = list(
            (
                await session.scalars(
                    select(VideoVariant).order_by(VideoVariant.aspect_ratio)
                )
            ).all()
        )

    assert render is not None
    assert render.status == "draft"
    assert (render.width, render.height) == (1920, 1080)
    assert Path(render.video_path).read_bytes() == b"test-video"
    assert "00:00:01,000 --> 00:00:02,500" in Path(render.subtitle_path).read_text()
    assert "stock_footage_query" in Path(render.timeline_path).read_text()
    assert [variant.aspect_ratio for variant in variants] == ["16:9", "9:16"]
    sizes = {(variant.width, variant.height) for variant in variants}
    assert sizes == {(1920, 1080), (1080, 1920)}
    for variant in variants:
        assert Path(variant.video_path).is_file()
        assert variant.style_key == "broadcast"
        assert variant.checks_json
    frame_dir = Path(render.video_path).parent / "frames" / "16x9"
    assert (frame_dir / "scene-001.jpg").is_file()
    await engine.dispose()

@pytest.mark.asyncio
async def test_video_generator_skips_a_narration_whose_audio_file_is_missing(
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
            url="https://bbc.example/stale",
            canonical_url="https://bbc.example/stale",
            title="Stale narration story",
            normalized_text="The narration file was removed from disk.",
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
            source_count=1,
            summarizer_name="test",
        )
        session.add(brief)
        await session.flush()
        script = NewsScript(
            article_id=article.id,
            research_brief_id=brief.id,
            status="draft",
            title=article.title,
            hook="Hook.",
            introduction="Introduction.",
            main_story=["Main story."],
            context=[],
            closing="Closing.",
            full_text="Full text.",
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
            NarrationAudio(
                script_id=script.id,
                scene_plan_id=plan.id,
                status="completed",
                provider_name="test",
                voice_name="test-voice",
                wav_path=str(tmp_path / "deleted.wav"),
                mp3_path=None,
                duration_seconds=2.5,
                sample_rate=16_000,
            )
        )
        await session.commit()

    renderer = FakeVideoRenderer()
    result = await VideoGeneratorAgent(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            video_output_dir=tmp_path / "video",
        ),
        sessions,
        renderer=renderer,
    ).execute()

    assert result.status == "completed"
    assert result.items_seen == 1
    assert result.items_created == 0
    assert any("no audio file" in warning for warning in result.warnings)
    assert renderer.calls == []
    async with sessions() as session:
        assert await session.scalar(select(VideoRender)) is None
    await engine.dispose()
