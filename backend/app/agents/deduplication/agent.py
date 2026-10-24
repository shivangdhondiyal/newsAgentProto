from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.deduplication.embeddings import (
    EmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
    cosine_similarity,
)
from backend.app.core.config import Settings
from backend.app.db.models import (
    AgentRun,
    Article,
    ArticleEmbedding,
    DuplicateCluster,
    DuplicateClusterMember,
)
from backend.app.schemas.deduplication import DeduplicationRunRead

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingCandidate:
    article_id: str
    vector: list[float]


class DeduplicationAgent:
    """Clusters semantically similar reports while retaining every original article."""

    name = "deduplication"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        provider: EmbeddingProvider | None = None,
    ) -> None:
        if settings.deduplication_review_threshold > settings.deduplication_auto_threshold:
            raise ValueError("Review threshold cannot exceed automatic-match threshold")
        self._settings = settings
        self._session_factory = session_factory
        self._provider = provider or SentenceTransformerEmbeddingProvider(
            settings.embedding_model_name, settings.embedding_batch_size
        )

    async def execute(self) -> DeduplicationRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                articles = await self._load_unembedded_articles(session)
                run.items_seen = len(articles)
                if articles:
                    vectors = await asyncio.to_thread(
                        self._provider.embed_documents,
                        [self._embedding_text(article) for article in articles],
                    )
                    if len(vectors) != len(articles):
                        raise RuntimeError(
                            "Embedding provider returned an unexpected number of vectors"
                        )
                    candidates = await self._load_candidates(session)
                    for article, vector in zip(articles, vectors, strict=True):
                        await self._store_embedding(session, article, vector)
                        match, similarity = self._best_match(vector, candidates)
                        if match and similarity >= self._settings.deduplication_review_threshold:
                            status = (
                                "confirmed"
                                if similarity >= self._settings.deduplication_auto_threshold
                                else "review"
                            )
                            await self._link_to_cluster(
                                session, match.article_id, article.id, similarity, status
                            )
                            run.items_updated += 1
                            key = f"{status}_clusters"
                            run.details = {**run.details, key: run.details.get(key, 0) + 1}
                        else:
                            run.items_created += 1
                        candidates.append(EmbeddingCandidate(article.id, vector))
                    run.status = "completed"
                else:
                    run.status = "completed"
                    run.details = {"message": "No new articles require embedding"}
            except Exception as exc:
                logger.exception("Deduplication run failed")
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise RuntimeError(run.errors[-1])
        return DeduplicationRunRead.model_validate(run)

    async def _load_unembedded_articles(self, session: AsyncSession) -> list[Article]:
        statement = (
            select(Article)
            .outerjoin(
                ArticleEmbedding,
                and_(
                    ArticleEmbedding.article_id == Article.id,
                    ArticleEmbedding.model_name == self._provider.model_name,
                ),
            )
            .where(ArticleEmbedding.id.is_(None))
            .order_by(Article.collected_at.asc())
            .limit(self._settings.embedding_batch_size)
        )
        return list((await session.scalars(statement)).all())

    async def _load_candidates(self, session: AsyncSession) -> list[EmbeddingCandidate]:
        statement = select(ArticleEmbedding).where(
            ArticleEmbedding.model_name == self._provider.model_name
        )
        embeddings = list((await session.scalars(statement)).all())
        return [EmbeddingCandidate(item.article_id, item.vector) for item in embeddings]

    async def _store_embedding(
        self, session: AsyncSession, article: Article, vector: list[float]
    ) -> None:
        text_hash = hashlib.sha256(self._embedding_text(article).encode("utf-8")).hexdigest()
        session.add(
            ArticleEmbedding(
                article_id=article.id,
                model_name=self._provider.model_name,
                text_hash=text_hash,
                vector=vector,
                dimensions=len(vector),
            )
        )

    async def _link_to_cluster(
        self,
        session: AsyncSession,
        candidate_article_id: str,
        incoming_article_id: str,
        similarity: float,
        status: str,
    ) -> None:
        existing_cluster_id = await session.scalar(
            select(DuplicateClusterMember.cluster_id).where(
                DuplicateClusterMember.article_id == candidate_article_id
            )
        )
        if existing_cluster_id:
            cluster = await session.get(DuplicateCluster, existing_cluster_id)
            if cluster and cluster.status != "confirmed" and status == "confirmed":
                cluster.status = "confirmed"
            session.add(
                DuplicateClusterMember(
                    cluster_id=existing_cluster_id,
                    article_id=incoming_article_id,
                    similarity=similarity,
                    role="duplicate",
                )
            )
            return

        cluster = DuplicateCluster(canonical_article_id=candidate_article_id, status=status)
        session.add(cluster)
        await session.flush()
        session.add_all(
            [
                DuplicateClusterMember(
                    cluster_id=cluster.id,
                    article_id=candidate_article_id,
                    similarity=1.0,
                    role="canonical",
                ),
                DuplicateClusterMember(
                    cluster_id=cluster.id,
                    article_id=incoming_article_id,
                    similarity=similarity,
                    role="duplicate",
                ),
            ]
        )

    @staticmethod
    def _best_match(
        vector: list[float], candidates: list[EmbeddingCandidate]
    ) -> tuple[EmbeddingCandidate | None, float]:
        if not candidates:
            return None, 0.0
        best = max(candidates, key=lambda item: cosine_similarity(vector, item.vector))
        return best, cosine_similarity(vector, best.vector)

    @staticmethod
    def _embedding_text(article: Article) -> str:
        return f"{article.title}\n\n{article.normalized_text}"[:12_000]
