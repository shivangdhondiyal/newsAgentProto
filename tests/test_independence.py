from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.credibility.agent import CredibilityAgent
from backend.app.agents.independence import filter_independent_articles
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    CredibilityAssessment,
    CredibilityEvidence,
    DuplicateCluster,
    DuplicateClusterMember,
)


class ClaimExtractor:
    name = "test"

    async def extract(self, article: Article) -> list[str]:
        return ["An earthquake struck the city"]


async def _article(
    session, source_id: str, url: str, title: str = "Earthquake strikes city"
) -> Article:
    article = Article(
        source_id=source_id,
        source_name=source_id,
        url=url,
        canonical_url=url,
        title=title,
        normalized_text=(
            "Officials said an earthquake struck the city. Emergency crews responded."
        ),
    )
    session.add(article)
    await session.flush()
    return article


async def _cluster(session, articles: list[Article], status: str = "confirmed") -> None:
    cluster = DuplicateCluster(canonical_article_id=articles[0].id, status=status)
    session.add(cluster)
    await session.flush()
    for index, article in enumerate(articles):
        session.add(
            DuplicateClusterMember(
                cluster_id=cluster.id,
                article_id=article.id,
                similarity=1.0 if index == 0 else 0.94,
                role="canonical" if index == 0 else "duplicate",
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_independence_filter_excludes_target_cluster_and_collapses_other_clusters() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        target = await _article(session, "bbc-world", "https://bbc.example/target")
        duplicate = await _article(session, "reuters", "https://reuters.example/dup")
        independent = await _article(
            session, "associated-press", "https://ap.example/independent"
        )
        other_one = await _article(session, "npr", "https://npr.example/one")
        other_two = await _article(session, "guardian", "https://guardian.example/two")
        await _cluster(session, [target, duplicate])
        await _cluster(session, [other_one, other_two])
        await session.commit()

    async with sessions() as session:
        target = await session.scalar(select(Article).where(Article.source_id == "bbc-world"))
        duplicate = await session.scalar(select(Article).where(Article.source_id == "reuters"))
        independent = await session.scalar(
            select(Article).where(Article.source_id == "associated-press")
        )
        other_one = await session.scalar(select(Article).where(Article.source_id == "npr"))
        other_two = await session.scalar(
            select(Article).where(Article.source_id == "guardian")
        )
        filtered = await filter_independent_articles(
            session, target.id, [duplicate, independent, other_one, other_two]
        )

    assert [article.source_id for article in filtered] == ["associated-press", "npr"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_credibility_does_not_count_syndicated_reports_as_independent(
    tmp_path: Path,
) -> None:
    # A file-backed database gives nested sessions their own connection. An in-memory
    # SQLite StaticPool would share one connection and commit the outer transaction.
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'credibility.sqlite').as_posix()}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        target = await _article(session, "bbc-world", "https://bbc.example/target")
        duplicate = await _article(session, "reuters", "https://reuters.example/duplicate")
        syndicated = await _article(session, "npr", "https://npr.example/syndicated")
        await _article(session, "associated-press", "https://ap.example/independent")
        await _cluster(session, [target, duplicate, syndicated])
        await session.commit()

    settings = Settings(
        database_url=database_url,
        trust_policy_file=Path("config/trust.yml"),
        credibility_confidence_threshold=0.75,
    )
    result = await CredibilityAgent(settings, sessions, ClaimExtractor()).execute()
    assert result.status == "completed"

    async with sessions() as session:
        assessment = await session.scalar(
            select(CredibilityAssessment).where(CredibilityAssessment.article_id == target.id)
        )
        evidence = list(
            (
                await session.scalars(
                    select(CredibilityEvidence).where(
                        CredibilityEvidence.assessment_id == assessment.id
                    )
                )
            ).all()
        )

    assert assessment is not None
    assert assessment.status == "needs_review"
    assert assessment.independent_source_count == 1
    assert {item.source_id for item in evidence} == {"associated-press"}
    assert assessment.confidence_score <= 0.65
    await engine.dispose()
