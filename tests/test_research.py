from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.agents.research.agent import ResearchAgent
from backend.app.agents.research.contracts import ResearchSourceCandidate
from backend.app.agents.research.providers import LocalCorpusResearchProvider, trim_snippet
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import Article, CredibilityAssessment, ResearchBrief, ResearchSource


class FakeResearchProvider:
    async def search(self, article: Article) -> list[ResearchSourceCandidate]:
        return [
            ResearchSourceCandidate(
                source_type="test",
                source_id="reuters",
                publisher="Reuters",
                url="https://reuters.example/context",
                title="Officials give earthquake update",
                snippet="Officials reported the event and response measures.",
                relevance_score=0.91,
            )
        ]


@pytest.mark.asyncio
async def test_research_only_processes_corroborated_articles_and_keeps_sources() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        eligible = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/eligible",
            canonical_url="https://bbc.example/eligible",
            title="Earthquake strikes city",
            normalized_text=(
                "Officials said an earthquake struck the city. Emergency crews responded."
            ),
        )
        ineligible = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/review",
            canonical_url="https://bbc.example/review",
            title="Unconfirmed report",
            normalized_text="A report is being reviewed.",
        )
        session.add_all([eligible, ineligible])
        await session.flush()
        session.add_all(
            [
                CredibilityAssessment(
                    article_id=eligible.id,
                    status="corroborated",
                    confidence_score=0.9,
                    explanation="Independent sources corroborated the report.",
                    claims=[],
                    independent_source_count=2,
                    extractor_name="test",
                ),
                CredibilityAssessment(
                    article_id=ineligible.id,
                    status="needs_review",
                    confidence_score=0.5,
                    explanation="Further review is needed.",
                    claims=[],
                    independent_source_count=1,
                    extractor_name="test",
                ),
            ]
        )
        await session.commit()

    agent = ResearchAgent(
        Settings(database_url="sqlite+aiosqlite:///:memory:"),
        sessions,
        provider=FakeResearchProvider(),
    )
    result = await agent.execute()

    assert result.status == "completed"
    assert result.items_seen == 1
    assert result.items_created == 1
    async with sessions() as session:
        briefs = list((await session.scalars(select(ResearchBrief))).all())
        sources = list((await session.scalars(select(ResearchSource))).all())

    assert len(briefs) == 1
    assert briefs[0].article_id == eligible.id
    assert briefs[0].headline == eligible.title
    assert len(sources) == 1
    assert sources[0].publisher == "Reuters"
    await engine.dispose()


@pytest.mark.asyncio
async def test_local_research_keeps_one_source_per_publisher() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    body = (
        "Officials said an earthquake struck the city. Emergency crews responded "
        "through the night and residents moved to temporary shelters."
    )

    def article(source_id: str, source_name: str, url: str, content: str = body) -> Article:
        return Article(
            source_id=source_id,
            source_name=source_name,
            url=url,
            canonical_url=url,
            title="Earthquake strikes city",
            normalized_text=content,
        )

    async with sessions() as session:
        target = article("bbc-world", "BBC News — World", "https://www.bbc.com/news/world-quake")
        session.add_all(
            [
                target,
                article(
                    "bbc-technology",
                    "BBC News — Technology",
                    "https://www.bbc.co.uk/news/technology-quake",
                ),
                article(
                    "reuters",
                    "Reuters",
                    "https://www.reuters.com/world/quake",
                ),
                article(
                    "associated-press",
                    "Associated Press",
                    "https://apnews.com/article/quake",
                ),
                article(
                    "weather-desk",
                    "Weather Desk",
                    "https://weather.example/forecast",
                    content="Sunshine and mild temperatures are expected this weekend.",
                ),
            ]
        )
        await session.commit()

    provider = LocalCorpusResearchProvider(
        sessions,
        max_sources=10,
        trust_policy=SourceTrustPolicy.from_file(Path("config/trust.yml")),
    )
    sources = await provider.search(target)

    assert {source.source_id for source in sources} == {
        "reuters",
        "associated-press",
    }
    await engine.dispose()


def test_trim_snippet_prefers_sentence_boundary_and_caps_length() -> None:
    text = "A" * 250 + ". " + "B" * 300
    assert trim_snippet(text) == "A" * 250 + "."
    assert trim_snippet("short text") == "short text"
