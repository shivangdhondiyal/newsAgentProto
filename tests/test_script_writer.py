from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.script_writer.agent import ScriptWriterAgent
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    CredibilityAssessment,
    NewsScript,
    ResearchBrief,
)


async def _create_eligible_article(
    session,
    *,
    source_count: int,
    key_facts: list[str] | None = None,
    context: list[str] | None = None,
) -> Article:
    article = Article(
        source_id="bbc-world",
        source_name="BBC News",
        url=f"https://bbc.example/story-{source_count}",
        canonical_url=f"https://bbc.example/story-{source_count}",
        title="Earthquake strikes city",
        normalized_text="Officials said an earthquake struck the city.",
    )
    session.add(article)
    await session.flush()
    session.add(
        CredibilityAssessment(
            article_id=article.id,
            status="corroborated",
            confidence_score=0.9,
            explanation="Two independent sources corroborated the report.",
            claims=["An earthquake struck the city."],
            independent_source_count=2,
            extractor_name="test",
        )
    )
    session.add(
        ResearchBrief(
            article_id=article.id,
            status="completed",
            headline=article.title,
            key_facts=(
                key_facts
                if key_facts is not None
                else ["Officials said an earthquake struck the city."]
            ),
            context=(context if context is not None else ["Emergency teams began a response."]),
            uncertainties=[],
            source_count=source_count,
            summarizer_name="test",
        )
    )
    return article


@pytest.mark.asyncio
async def test_script_writer_creates_neutral_structured_draft() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = await _create_eligible_article(session, source_count=2)
        await session.commit()

    agent = ScriptWriterAgent(Settings(database_url="sqlite+aiosqlite:///:memory:"), sessions)
    result = await agent.execute()

    assert result.status == "completed"
    assert result.details["drafts"] == 1
    async with sessions() as session:
        script = await session.scalar(select(NewsScript))

    assert script is not None
    assert script.article_id == article.id
    assert script.status == "draft"
    assert script.main_story == ["Officials said an earthquake struck the city."]
    assert "shocking" not in script.full_text.lower()
    await engine.dispose()


@pytest.mark.asyncio
async def test_script_writer_flags_limited_research_context() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await _create_eligible_article(session, source_count=0)
        await session.commit()

    agent = ScriptWriterAgent(Settings(database_url="sqlite+aiosqlite:///:memory:"), sessions)
    result = await agent.execute()

    assert result.details["needs_review"] == 1
    async with sessions() as session:
        script = await session.scalar(select(NewsScript))

    assert script is not None
    assert script.status == "needs_review"
    assert script.review_reasons == [
        "Insufficient additional research sources for automatic draft routing."
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_script_writer_trims_context_and_main_story_to_target() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fact = (
        "Officials said the earthquake struck the city while emergency crews responded "
        "through the night and residents moved to temporary shelters."
    )
    context = ["Additional background sentence about the response and recovery effort. " * 10]
    async with sessions() as session:
        await _create_eligible_article(
            session,
            source_count=2,
            key_facts=[fact] * 4,
            context=context,
        )
        await session.commit()

    agent = ScriptWriterAgent(
        Settings(database_url="sqlite+aiosqlite:///:memory:", script_target_seconds=30),
        sessions,
    )
    result = await agent.execute()

    assert result.status == "completed"
    async with sessions() as session:
        script = await session.scalar(select(NewsScript))

    assert script is not None
    assert script.status == "draft"
    assert script.context == []
    assert len(script.main_story) == 1
    assert script.estimated_duration_seconds <= 33
    await engine.dispose()
