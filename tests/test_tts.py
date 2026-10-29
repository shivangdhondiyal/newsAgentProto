from __future__ import annotations

import wave
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.errors import AgentRunFailed
from backend.app.agents.tts.agent import TTSAgent
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    ContentApproval,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    ResearchBrief,
    ScenePlan,
    VideoRender,
    VideoVariant,
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
        audio_master_enabled=False,
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
    # Each fake chunk is one second long, plus the configured pauses.
    assert narration.duration_seconds > len(chunks)
    assert chunks[0].start_seconds == 0.0
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.start_seconds > previous.start_seconds
        assert current.start_seconds >= previous.start_seconds + previous.duration_seconds
    assert all(chunk.duration_seconds == 1.0 for chunk in chunks)
    assert all(Path(chunk.wav_path).is_file() for chunk in chunks)
    await engine.dispose()


@pytest.mark.asyncio
async def test_forced_renarration_invalidates_draft_render(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/renarrate",
            canonical_url="https://bbc.example/renarrate",
            title="Protests continue in France",
            normalized_text="Protests continued across France on Tuesday.",
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
            hook="Protests continued across France.",
            introduction="Police reported scattered clashes.",
            main_story=["Unions called for a general strike."],
            context=["Talks resume on Wednesday."],
            closing="More verified information may follow.",
            full_text=(
                "Protests continued across France. Police reported scattered clashes. "
                "Unions called for a general strike. Talks resume on Wednesday. "
                "More verified information may follow."
            ),
            estimated_duration_seconds=30,
            target_duration_seconds=60,
            writer_name="test",
            review_reasons=[],
        )
        session.add(script)
        await session.flush()
        plan = ScenePlan(
            script_id=script.id,
            status="draft",
            total_duration_seconds=30,
            planner_name="test",
        )
        session.add(plan)
        await session.flush()
        old_narration = NarrationAudio(
            script_id=script.id,
            scene_plan_id=plan.id,
            status="completed",
            provider_name="test",
            voice_name="test-voice",
            wav_path=str(tmp_path / "old.wav"),
            duration_seconds=20.0,
            sample_rate=16_000,
        )
        session.add(old_narration)
        await session.flush()
        render = VideoRender(
            narration_audio_id=old_narration.id,
            scene_plan_id=plan.id,
            status="draft",
            renderer_name="test",
            video_path="assets/generated/video/old/draft.mp4",
            subtitle_path="assets/generated/video/old/captions.srt",
            timeline_path="assets/generated/video/old/timeline.json",
            duration_seconds=20.0,
            width=640,
            height=360,
            fps=30,
        )
        session.add(render)
        await session.flush()
        session.add(
            VideoVariant(
                video_render_id=render.id,
                aspect_ratio="16:9",
                style_key="broadcast",
                width=640,
                height=360,
                fps=30,
                video_path="assets/generated/video/old/draft.mp4",
                subtitle_path="assets/generated/video/old/captions.srt",
                timeline_path="assets/generated/video/old/timeline.json",
                duration_seconds=20.0,
            )
        )
        session.add(
            ContentApproval(video_render_id=render.id, status="pending", reviewer="operator")
        )
        await session.commit()

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        tts_output_dir=tmp_path / "audio",
        tts_chunk_max_characters=80,
        tts_generate_mp3=False,
        audio_master_enabled=False,
    )
    result = await TTSAgent(settings, sessions, FakeSpeechSynthesizer()).execute(
        script_ids=[script.id], force=True
    )

    assert result.status == "completed"
    async with sessions() as session:
        narration = await session.scalar(
            select(NarrationAudio).where(NarrationAudio.script_id == script.id)
        )
        assert narration is not None
        assert narration.id != old_narration.id
        assert await session.scalar(select(VideoRender)) is None
        assert await session.scalar(select(VideoVariant)) is None
        assert await session.scalar(select(ContentApproval)) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_forced_renarration_refuses_approved_render(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/approved",
            canonical_url="https://bbc.example/approved",
            title="Approved story",
            normalized_text="The approved story text.",
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
            hook="An approved story.",
            introduction="It already passed review.",
            main_story=["The render was approved by an editor."],
            context=["Re-narration must not delete it."],
            closing="More verified information may follow.",
            full_text=(
                "An approved story. It already passed review. "
                "The render was approved by an editor. "
                "Re-narration must not delete it. More verified information may follow."
            ),
            estimated_duration_seconds=30,
            target_duration_seconds=60,
            writer_name="test",
            review_reasons=[],
        )
        session.add(script)
        await session.flush()
        plan = ScenePlan(
            script_id=script.id,
            status="draft",
            total_duration_seconds=30,
            planner_name="test",
        )
        session.add(plan)
        await session.flush()
        narration = NarrationAudio(
            script_id=script.id,
            scene_plan_id=plan.id,
            status="completed",
            provider_name="test",
            voice_name="test-voice",
            wav_path=str(tmp_path / "approved.wav"),
            duration_seconds=20.0,
            sample_rate=16_000,
        )
        session.add(narration)
        await session.flush()
        render = VideoRender(
            narration_audio_id=narration.id,
            scene_plan_id=plan.id,
            status="draft",
            renderer_name="test",
            video_path="assets/generated/video/approved/draft.mp4",
            subtitle_path="assets/generated/video/approved/captions.srt",
            timeline_path="assets/generated/video/approved/timeline.json",
            duration_seconds=20.0,
            width=640,
            height=360,
            fps=30,
        )
        session.add(render)
        await session.flush()
        session.add(
            ContentApproval(video_render_id=render.id, status="approved", reviewer="editor")
        )
        await session.commit()

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        tts_output_dir=tmp_path / "audio",
        tts_chunk_max_characters=80,
        tts_generate_mp3=False,
        audio_master_enabled=False,
    )
    with pytest.raises(AgentRunFailed, match="approved"):
        await TTSAgent(settings, sessions, FakeSpeechSynthesizer()).execute(
            script_ids=[script.id], force=True
        )

    async with sessions() as session:
        assert await session.scalar(select(NarrationAudio)) is not None
        assert await session.scalar(select(VideoRender)) is not None
        assert await session.scalar(select(ContentApproval)) is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_narration_reads_the_planned_scene_copy(tmp_path: Path) -> None:
    from backend.app.db.models import Scene

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/planned",
            canonical_url="https://bbc.example/planned",
            title="Truce holds in Oleshky",
            normalized_text="Fighting stopped after a truce.",
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
            hook="Here is what is known so far about the truce.",
            introduction="This report is based on information corroborated by 3 sources.",
            main_story=["Fighting in the city has stopped after a truce."],
            context=[],
            closing="This report reflects information available from the cited sources.",
            full_text=(
                "Here is what is known so far about the truce. This report is based on "
                "information corroborated by 3 sources. Fighting in the city has stopped "
                "after a truce. This report reflects information available from the cited "
                "sources."
            ),
            estimated_duration_seconds=30,
            target_duration_seconds=60,
            writer_name="test",
            review_reasons=[],
        )
        session.add(script)
        await session.flush()
        plan = ScenePlan(
            script_id=script.id,
            status="draft",
            total_duration_seconds=30,
            planner_name="test",
        )
        session.add(plan)
        await session.flush()
        session.add_all(
            [
                Scene(
                    scene_plan_id=plan.id,
                    position=1,
                    start_seconds=0,
                    duration_seconds=5,
                    narration="Fighting in the city has stopped after a truce.",
                    subtitle="Fighting in the city has stopped after a truce.",
                    scene_description="City view.",
                    stock_footage_query="city truce",
                    image_search_query="city truce",
                    animation_suggestion="Slow zoom.",
                ),
                Scene(
                    scene_plan_id=plan.id,
                    position=2,
                    start_seconds=5,
                    duration_seconds=5,
                    narration="Aid convoys are expected on Friday.",
                    subtitle="Aid convoys are expected on Friday.",
                    scene_description="Aid convoy.",
                    stock_footage_query="aid convoy",
                    image_search_query="aid convoy",
                    animation_suggestion="Slow zoom.",
                ),
            ]
        )
        await session.commit()

        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            tts_output_dir=tmp_path / "audio",
            tts_chunk_max_characters=80,
            tts_generate_mp3=False,
            audio_master_enabled=False,
        )
        agent = TTSAgent(settings, sessions, FakeSpeechSynthesizer())
        text = await agent._narration_text(session, script, plan)

    # The voiceover must match the cleaned scene copy, not the raw script with
    # its production boilerplate.
    assert text == (
        "Fighting in the city has stopped after a truce.\n\nAid convoys are expected on Friday."
    )
    await engine.dispose()
