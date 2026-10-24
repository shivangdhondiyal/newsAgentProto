from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.deduplication.agent import DeduplicationAgent
from backend.app.agents.deduplication.embeddings import cosine_similarity
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import Article, DuplicateCluster, DuplicateClusterMember


class FakeEmbeddingProvider:
    model_name = "test-model"

    def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for document in documents:
            if "earthquake" in document.lower():
                vectors.append([1.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0])
        return vectors


def test_cosine_similarity_handles_matches_and_invalid_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0


@pytest.mark.asyncio
async def test_deduplication_clusters_similar_articles_without_deleting_them() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all(
            [
                Article(
                    source_id="a",
                    source_name="A",
                    url="https://a.example/one",
                    canonical_url="https://a.example/one",
                    title="Earthquake reported in city",
                    normalized_text="Officials report an earthquake in the city.",
                ),
                Article(
                    source_id="b",
                    source_name="B",
                    url="https://b.example/two",
                    canonical_url="https://b.example/two",
                    title="City earthquake update",
                    normalized_text="A strong earthquake was reported by officials.",
                ),
                Article(
                    source_id="c",
                    source_name="C",
                    url="https://c.example/three",
                    canonical_url="https://c.example/three",
                    title="Football final announced",
                    normalized_text="The sports federation announced the final.",
                ),
            ]
        )
        await session.commit()

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        embedding_batch_size=10,
        deduplication_auto_threshold=0.88,
        deduplication_review_threshold=0.78,
    )
    result = await DeduplicationAgent(settings, sessions, FakeEmbeddingProvider()).execute()

    assert result.status == "completed"
    assert result.items_created == 2
    assert result.items_updated == 1
    assert result.details["confirmed_clusters"] == 1
    async with sessions() as session:
        clusters = list((await session.scalars(select(DuplicateCluster))).all())
        members = list((await session.scalars(select(DuplicateClusterMember))).all())
        articles = list((await session.scalars(select(Article))).all())

    assert len(clusters) == 1
    assert clusters[0].status == "confirmed"
    assert len(members) == 2
    assert len(articles) == 3
    await engine.dispose()
