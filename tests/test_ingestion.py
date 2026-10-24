from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.base import Base
from backend.app.schemas.article import CollectedArticle
from backend.app.services.ingestion import ArticleIngestionService


@pytest.mark.asyncio
async def test_ingestion_is_idempotent_for_canonical_url() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    article = CollectedArticle(
        source_id="example",
        source_name="Example",
        url="https://example.com/story?utm_source=rss",
        canonical_url="https://example.com/story",
        title="Story",
        normalized_text="A useful report.",
        published_at=datetime.now(UTC),
    )
    async with sessions() as session:
        service = ArticleIngestionService()
        _, created = await service.upsert(session, article)
        await session.commit()
        _, created_again = await service.upsert(session, article)
        await session.commit()

    assert created is True
    assert created_again is False
    await engine.dispose()
