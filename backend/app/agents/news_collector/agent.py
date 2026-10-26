from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.errors import AgentRunFailed
from backend.app.collectors.rss import RSSCollector, RSSSource
from backend.app.core.config import Settings
from backend.app.db.models import AgentRun
from backend.app.schemas.article import CollectionRunRead
from backend.app.services.ingestion import ArticleIngestionService

logger = logging.getLogger(__name__)


class NewsCollectorAgent:
    """Collects configured feeds and records a complete audit trail for each run."""

    name = "news_collector"

    def __init__(
        self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._ingestion = ArticleIngestionService()

    async def execute(self) -> CollectionRunRead:
        started_at = datetime.now(UTC)
        clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                sources = self._load_sources(self._settings.sources_file)
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds,
                    headers={"User-Agent": self._settings.user_agent},
                    follow_redirects=True,
                ) as client:
                    collector = RSSCollector(client, self._settings.max_articles_per_source)
                    for source in sources:
                        await self._collect_source(session, collector, source, run)
                run.status = "completed" if not run.errors else "completed_with_errors"
            except Exception as exc:
                # Keep the run auditable, then surface the failure to API callers.
                logger.exception("News collection run failed")
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise AgentRunFailed(self.name, run.id, run.errors[-1], run.errors)
        return CollectionRunRead(
            id=run.id,
            status=run.status,
            items_seen=run.items_seen,
            items_created=run.items_created,
            items_updated=run.items_updated,
            warnings=run.warnings,
            errors=run.errors,
            details=run.details,
        )

    async def _collect_source(
        self, session: AsyncSession, collector: RSSCollector, source: RSSSource, run: AgentRun
    ) -> None:
        try:
            articles, warnings = await collector.collect(source)
            run.warnings = [*run.warnings, *warnings]
            run.items_seen += len(articles)
            for article in articles:
                _, created = await self._ingestion.upsert(session, article)
                if created:
                    run.items_created += 1
                else:
                    run.items_updated += 1
            await session.commit()
        except Exception as exc:
            logger.exception("Source collection failed", extra={"source_id": source.id})
            run.errors = [*run.errors, f"{source.id}: {exc}"]

    @staticmethod
    def _load_sources(path: Path) -> list[RSSSource]:
        if not path.is_file():
            raise FileNotFoundError(f"Source configuration not found: {path}")
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        configured = payload.get("sources", [])
        return [source for item in configured if (source := RSSSource.model_validate(item)).enabled]
