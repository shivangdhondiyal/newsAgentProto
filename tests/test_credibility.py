from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.credibility.agent import CredibilityAgent
from backend.app.agents.credibility.contracts import EvidenceCandidate
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import Article, CredibilityAssessment, CredibilityEvidence


class FakeClaimExtractor:
    name = "test-extractor"

    async def extract(self, article: Article) -> list[str]:
        return ["An earthquake struck the city"]


class TwoSourceEvidenceFinder:
    async def find(self, claim: str, article: Article) -> list[EvidenceCandidate]:
        return [
            EvidenceCandidate(
                source_id="reuters",
                publisher="Reuters",
                url="https://reuters.example/report",
                title="Earthquake strikes city",
                snippet="Officials confirmed the event.",
                relevance_score=0.95,
            ),
            EvidenceCandidate(
                source_id="associated-press",
                publisher="Associated Press",
                url="https://ap.example/report",
                title="City reports earthquake",
                snippet="Emergency services responded.",
                relevance_score=0.92,
            ),
        ]


class OneSourceEvidenceFinder:
    async def find(self, claim: str, article: Article) -> list[EvidenceCandidate]:
        return [
            EvidenceCandidate(
                source_id="reuters",
                publisher="Reuters",
                url="https://reuters.example/report",
                title="Earthquake strikes city",
                snippet=None,
                relevance_score=1.0,
            )
        ]


class SamePublisherEvidenceFinder:
    async def find(self, claim: str, article: Article) -> list[EvidenceCandidate]:
        return [
            EvidenceCandidate(
                source_id="bbc-technology",
                publisher="BBC News — Technology",
                url="https://www.bbc.co.uk/news/technology-quake",
                title="Earthquake strikes city",
                snippet="Officials confirmed the event.",
                relevance_score=0.98,
            ),
            EvidenceCandidate(
                source_id="associated-press",
                publisher="Associated Press",
                url="https://apnews.com/article/quake",
                title="City reports earthquake",
                snippet="Emergency services responded.",
                relevance_score=0.92,
            ),
        ]


async def _make_agent(evidence_finder: object):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            Article(
                source_id="bbc-world",
                source_name="BBC News — World",
                url="https://www.bbc.com/news/world-quake",
                canonical_url="https://www.bbc.com/news/world-quake",
                title="Earthquake strikes city",
                normalized_text="Officials said an earthquake struck the city today.",
            )
        )
        await session.commit()
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    agent = CredibilityAgent(settings, sessions, FakeClaimExtractor(), evidence_finder)
    return engine, sessions, agent


@pytest.mark.asyncio
async def test_credibility_requires_two_independent_sources_and_preserves_evidence() -> None:
    engine, sessions, agent = await _make_agent(TwoSourceEvidenceFinder())
    result = await agent.execute()

    assert result.status == "completed"
    assert result.details["corroborated"] == 1
    async with sessions() as session:
        assessment = await session.scalar(select(CredibilityAssessment))
        evidence = list((await session.scalars(select(CredibilityEvidence))).all())

    assert assessment is not None
    assert assessment.status == "corroborated"
    assert assessment.independent_source_count == 2
    assert len(evidence) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_credibility_routes_single_source_to_manual_review() -> None:
    engine, sessions, agent = await _make_agent(OneSourceEvidenceFinder())
    await agent.execute()
    async with sessions() as session:
        assessment = await session.scalar(select(CredibilityAssessment))

    assert assessment is not None
    assert assessment.status == "needs_review"
    assert assessment.confidence_score <= 0.65
    await engine.dispose()


@pytest.mark.asyncio
async def test_credibility_counts_two_feeds_from_one_publisher_once() -> None:
    engine, sessions, agent = await _make_agent(SamePublisherEvidenceFinder())
    await agent.execute()
    async with sessions() as session:
        assessment = await session.scalar(select(CredibilityAssessment))
        evidence = list((await session.scalars(select(CredibilityEvidence))).all())

    assert assessment is not None
    assert assessment.status == "needs_review"
    assert assessment.independent_source_count == 1
    assert assessment.confidence_score <= 0.65
    assert {item.source_id for item in evidence} == {
        "bbc-technology",
        "associated-press",
    }
    await engine.dispose()
