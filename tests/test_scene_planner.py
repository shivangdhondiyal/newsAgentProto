from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.scene_planner.agent import ScenePlannerAgent
from backend.app.agents.scene_planner.planner import RuleBasedScenePlanner
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import Article, NewsScript, ResearchBrief, Scene, ScenePlan


async def _add_script(session, *, status: str) -> NewsScript:
    article = Article(
        source_id="bbc-world",
        source_name="BBC News",
        url=f"https://bbc.example/{status}",
        canonical_url=f"https://bbc.example/{status}",
        title="Earthquake strikes city",
        normalized_text="Officials said an earthquake struck the city.",
    )
    session.add(article)
    await session.flush()
    brief = ResearchBrief(
        article_id=article.id,
        status="completed",
        headline=article.title,
        key_facts=["Officials said an earthquake struck the city."],
        context=["Emergency teams began a response."],
        uncertainties=[],
        source_count=2,
        summarizer_name="test",
    )
    session.add(brief)
    await session.flush()
    script = NewsScript(
        article_id=article.id,
        research_brief_id=brief.id,
        status=status,
        title=article.title,
        hook="Here is what is known so far.",
        introduction="The report is corroborated by independent sources.",
        main_story=["Officials said an earthquake struck the city."],
        context=["Emergency teams began a response."],
        closing="The report will be updated as information becomes available.",
        full_text="A neutral script.",
        estimated_duration_seconds=30,
        target_duration_seconds=60,
        writer_name="test",
        review_reasons=[],
    )
    session.add(script)
    return script


def test_scene_subtitle_drops_credibility_boilerplate() -> None:
    planner = RuleBasedScenePlanner()
    scene = planner._make_scene(
        "This report is based on information corroborated by 3 independent sources. "
        "Officials said an earthquake struck the city.",
        2,
    )
    assert "corroborated" not in scene.subtitle.lower()
    assert "earthquake" in scene.subtitle.lower()
    assert "corroborated" in scene.narration.lower()
    assert scene.keywords


def test_boilerplate_only_scene_uses_the_story_headline() -> None:
    planner = RuleBasedScenePlanner()
    scene = planner._make_scene(
        "This report is based on information corroborated by 3 independent sources.",
        2,
        fallback_display="Earthquake strikes the city",
    )
    assert scene.subtitle == "Earthquake strikes the city"
    assert "corroborated" in scene.narration.lower()


@pytest.mark.asyncio
async def test_scene_planner_creates_ordered_editable_timeline_for_draft_script() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        script = await _add_script(session, status="draft")
        await session.commit()

    result = await ScenePlannerAgent(
        Settings(database_url="sqlite+aiosqlite:///:memory:"), sessions
    ).execute()

    assert result.status == "completed"
    assert result.items_created == 1
    async with sessions() as session:
        plan = await session.scalar(select(ScenePlan))
        scenes = list((await session.scalars(select(Scene).order_by(Scene.position))).all())

    assert plan is not None
    assert plan.script_id == script.id
    assert [scene.position for scene in scenes] == [1, 2, 3, 4]
    assert scenes[0].start_seconds == 0
    assert scenes[-1].start_seconds + scenes[-1].duration_seconds == plan.total_duration_seconds
    assert all(scene.stock_footage_query for scene in scenes)
    await engine.dispose()


def test_planner_drops_boilerplate_segments_and_keeps_spoken_copy_clean() -> None:
    planner = RuleBasedScenePlanner()

    class _Script:
        title = "Truce holds in Oleshky"
        hook = "Here is what is known so far about Truce holds in Oleshky."
        introduction = "This report is based on information corroborated by 3 sources."
        main_story = [
            "Truce holds in Oleshky Fighting in the city has stopped after a truce.",
            "Aid convoys are expected on Friday.",
        ]
        context: list[str] = []
        closing = "This report reflects information available from the cited sources at this time."

    segments = planner._segments(_Script())

    assert segments == [
        "Truce holds in Oleshky.",
        "Fighting in the city has stopped after a truce.",
        "Aid convoys are expected on Friday.",
    ]
    assert all("corroborated" not in segment.lower() for segment in segments)
    assert all("what is known so far" not in segment.lower() for segment in segments)


@pytest.mark.asyncio
async def test_scene_planner_excludes_scripts_marked_for_review() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await _add_script(session, status="needs_review")
        await session.commit()

    result = await ScenePlannerAgent(
        Settings(database_url="sqlite+aiosqlite:///:memory:"), sessions
    ).execute()

    assert result.status == "completed"
    assert result.items_seen == 0
    async with sessions() as session:
        assert await session.scalar(select(ScenePlan)) is None
    await engine.dispose()
