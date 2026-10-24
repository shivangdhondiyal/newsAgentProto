from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Article
from backend.app.schemas.article import CollectedArticle


class ArticleIngestionService:
    """Idempotently persist collected input; semantic deduplication belongs to Phase 2."""

    async def upsert(
        self, session: AsyncSession, incoming: CollectedArticle
    ) -> tuple[Article, bool]:
        existing = await session.scalar(
            select(Article).where(Article.canonical_url == incoming.canonical_url)
        )
        values = {
            "source_id": incoming.source_id,
            "source_name": incoming.source_name,
            "url": str(incoming.url),
            "canonical_url": incoming.canonical_url,
            "title": incoming.title,
            "summary": incoming.summary,
            "raw_html": incoming.raw_html,
            "normalized_text": incoming.normalized_text,
            "language": incoming.language,
            "category": incoming.category,
            "country": incoming.country,
            "published_at": incoming.published_at,
            "metadata_json": incoming.metadata,
        }
        if existing:
            for field, value in values.items():
                setattr(existing, field, value)
            return existing, False

        article = Article(**values)
        session.add(article)
        return article, True
