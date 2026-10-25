from __future__ import annotations

import logging
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.agents.research.contracts import ResearchProvider, ResearchSummarizer
from backend.app.agents.research.providers import (
    CompositeResearchProvider,
    GdeltResearchProvider,
    LocalCorpusResearchProvider,
)
from backend.app.agents.research.summarizer import ExtractiveResearchSummarizer
from backend.app.core.config import Settings
from backend.app.db.models import (
    AgentRun,
    Article,
    CredibilityAssessment,
    ResearchBrief,
    ResearchSource,
)
from backend.app.schemas.research import ResearchRunRead

logger = logging.getLogger(__name__)


class ResearchAgent:
    """Creates auditable, extractive background briefs for corroborated reports only."""

    name = "research"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        provider: ResearchProvider | None = None,
        summarizer: ResearchSummarizer | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._provider = provider or self._create_provider(settings, session_factory)
        self._summarizer = summarizer or ExtractiveResearchSummarizer()

    async def execute(self) -> ResearchRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                articles = await self._load_researchable_articles(session)
                run.items_seen = len(articles)
                for article in articles:
                    sources = await self._provider.search(article)
                    summary = self._summarizer.summarize(article, sources)
                    brief = ResearchBrief(
                        article_id=article.id,
                        status="completed",
                        headline=summary.headline,
                        key_facts=summary.key_facts,
                        context=summary.context,
                        uncertainties=summary.uncertainties,
                        source_count=len(sources),
                        summarizer_name=self._summarizer.name,
                    )
                    session.add(brief)
                    await session.flush()
                    session.add_all(
                        [
                            ResearchSource(
                                brief_id=brief.id,
                                source_type=source.source_type,
                                source_id=source.source_id,
                                publisher=source.publisher,
                                url=source.url,
                                title=source.title,
                                snippet=source.snippet,
                                relevance_score=source.relevance_score,
                            )
                            for source in sources
                        ]
                    )
                    run.items_created += 1
                    if sources:
                        run.details = {
                            **run.details,
                            "briefs_with_context": run.details.get("briefs_with_context", 0) + 1,
                        }
                    else:
                        run.warnings = [
                            *run.warnings,
                            f"{article.id}: no additional context source was retrieved",
                        ]
                run.status = "completed"
            except Exception as exc:
                logger.exception("Research run failed")
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise RuntimeError(run.errors[-1])
        return ResearchRunRead.model_validate(run)

    async def _load_researchable_articles(self, session: AsyncSession) -> list[Article]:
        statement = (
            select(Article)
            .join(CredibilityAssessment, CredibilityAssessment.article_id == Article.id)
            .outerjoin(ResearchBrief, ResearchBrief.article_id == Article.id)
            .where(CredibilityAssessment.status == "corroborated", ResearchBrief.id.is_(None))
            .order_by(Article.collected_at.asc())
            .limit(self._settings.research_batch_size)
        )
        return list((await session.scalars(statement)).all())

    @staticmethod
    def _create_provider(
        settings: Settings, session_factory: async_sessionmaker[AsyncSession]
    ) -> ResearchProvider:
        trust_policy = SourceTrustPolicy.from_file(settings.trust_policy_file)
        providers: list[ResearchProvider] = [
            LocalCorpusResearchProvider(
                session_factory, settings.research_max_sources, trust_policy
            )
        ]
        if settings.research_external_provider == "gdelt":
            providers.append(GdeltResearchProvider(trust_policy, settings.research_max_sources))
        elif settings.research_external_provider != "none":
            raise ValueError("Unsupported research external provider; use 'none' or 'gdelt'")
        return CompositeResearchProvider(providers)
