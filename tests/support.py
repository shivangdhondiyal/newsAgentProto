from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    CredibilityAssessment,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    ResearchBrief,
    ResearchSource,
    Scene,
    ScenePlan,
    VideoRender,
)


@dataclass
class SeededContent:
    engine: AsyncEngine
    sessions: async_sessionmaker
    settings: Settings
    article_id: str
    script_id: str
    scene_id: str
    narration_id: str
    video_id: str
    video_path: Path


async def seed_content(
    tmp_path: Path,
    *,
    database_url: str = "sqlite+aiosqlite:///:memory:",
    video_bytes: int = 4_096,
    script_status: str = "draft",
    review_reasons: list[str] | None = None,
) -> SeededContent:
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url=database_url,
        video_output_dir=tmp_path / "video",
        tts_output_dir=tmp_path / "audio",
        publishing_output_dir=tmp_path / "published",
        approval_min_video_bytes=1_024,
    )
    async with sessions() as session:
        article = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/story",
            canonical_url="https://bbc.example/story",
            title="Earthquake strikes city",
            summary="Officials reported an earthquake and a coordinated response.",
            normalized_text=(
                "Officials said an earthquake struck the city. Emergency crews responded. "
                "Independent reports corroborated the initial account."
            ),
            language="en",
            category="world",
            country="GB",
        )
        session.add(article)
        await session.flush()
        session.add(
            CredibilityAssessment(
                article_id=article.id,
                status="corroborated",
                confidence_score=0.91,
                explanation=(
                    "Two independent trusted sources produced relevant corroboration. "
                    "This is an evidence-based confidence estimate, not proof of fact."
                ),
                claims=["An earthquake struck the city"],
                independent_source_count=2,
                extractor_name="test",
            )
        )
        brief = ResearchBrief(
            article_id=article.id,
            status="completed",
            headline=article.title,
            key_facts=["Officials reported the earthquake."],
            context=["Emergency crews responded."],
            uncertainties=["Casualty figures were not yet confirmed."],
            source_count=2,
            summarizer_name="test",
        )
        session.add(brief)
        await session.flush()
        session.add_all(
            [
                ResearchSource(
                    brief_id=brief.id,
                    source_type="test",
                    source_id="reuters",
                    publisher="Reuters",
                    url="https://reuters.example/report",
                    title="City earthquake report",
                    snippet="Officials confirmed the event.",
                    relevance_score=0.95,
                ),
                ResearchSource(
                    brief_id=brief.id,
                    source_type="test",
                    source_id="associated-press",
                    publisher="Associated Press",
                    url="https://ap.example/report",
                    title="Earthquake response begins",
                    snippet="Emergency services responded.",
                    relevance_score=0.9,
                ),
            ]
        )
        script = NewsScript(
            article_id=article.id,
            research_brief_id=brief.id,
            status=script_status,
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
            review_reasons=review_reasons or [],
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
        scene = Scene(
            scene_plan_id=plan.id,
            position=1,
            start_seconds=0,
            duration_seconds=30,
            narration="Officials said an earthquake struck the city.",
            subtitle="Officials said an earthquake struck the city.",
            keywords=["earthquake", "city", "response"],
            scene_description="Neutral map visual.",
            stock_footage_query="earthquake city neutral news footage",
            image_search_query="earthquake city editorial image",
            animation_suggestion="Slow zoom.",
            transition="fade",
        )
        session.add(scene)
        narration = NarrationAudio(
            script_id=script.id,
            scene_plan_id=plan.id,
            status="completed",
            provider_name="test",
            voice_name="test-voice",
            wav_path=str(tmp_path / "audio" / script.id / "narration.wav"),
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
                    wav_path=str(tmp_path / "audio" / script.id / "chunk-001.wav"),
                    start_seconds=0.0,
                    duration_seconds=1.0,
                ),
                NarrationChunk(
                    narration_audio_id=narration.id,
                    position=2,
                    text="Emergency crews began their response.",
                    wav_path=str(tmp_path / "audio" / script.id / "chunk-002.wav"),
                    start_seconds=1.0,
                    duration_seconds=1.5,
                ),
            ]
        )
        video_dir = tmp_path / "video" / narration.id
        video_dir.mkdir(parents=True, exist_ok=True)
        video_path = video_dir / "draft.mp4"
        video_path.write_bytes(b"x" * video_bytes)
        (video_dir / "subtitles.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nFirst line\n\n"
            "2\n00:00:01,000 --> 00:00:02,500\nSecond line\n",
            encoding="utf-8",
        )
        (video_dir / "timeline.json").write_text(
            json.dumps([{"position": 1, "scene_description": "Neutral map"}]),
            encoding="utf-8",
        )
        render = VideoRender(
            narration_audio_id=narration.id,
            scene_plan_id=plan.id,
            status="draft",
            renderer_name="test",
            video_path=str(video_path),
            subtitle_path=str(video_dir / "subtitles.srt"),
            timeline_path=str(video_dir / "timeline.json"),
            duration_seconds=2.5,
            width=1920,
            height=1080,
            fps=30,
        )
        session.add(render)
        await session.commit()
        return SeededContent(
            engine=engine,
            sessions=sessions,
            settings=settings,
            article_id=article.id,
            script_id=script.id,
            scene_id=scene.id,
            narration_id=narration.id,
            video_id=render.id,
            video_path=video_path,
        )
