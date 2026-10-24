from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.script_writer.contracts import ScriptDraft, ScriptWriter
from backend.app.agents.script_writer.writers import ExtractiveScriptWriter, OllamaScriptWriter
from backend.app.core.config import Settings
from backend.app.db.models import (
    AgentRun,
    Article,
    CredibilityAssessment,
    NewsScript,
    ResearchBrief,
)
from backend.app.schemas.script import ScriptRunRead

logger = logging.getLogger(__name__)
SENSATIONAL_TERMS = {"breaking", "shocking", "outrageous", "must-see", "unbelievable"}


class ScriptWriterAgent:
    """Generates a neutral narration draft from persisted, corroborated research only."""

    name = "script_writer"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        writer: ScriptWriter | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._writer = writer or self._create_writer(settings)

    async def execute(self) -> ScriptRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                candidates = await self._load_eligible_articles(session)
                run.items_seen = len(candidates)
                for article, assessment, brief in candidates:
                    draft = await self._writer.write(article, assessment, brief)
                    review_reasons = self._review_reasons(draft, brief)
                    full_text = self._render(draft)
                    script = NewsScript(
                        article_id=article.id,
                        research_brief_id=brief.id,
                        status="needs_review" if review_reasons else "draft",
                        title=draft.title,
                        hook=draft.hook,
                        introduction=draft.introduction,
                        main_story=draft.main_story,
                        context=draft.context,
                        closing=draft.closing,
                        full_text=full_text,
                        estimated_duration_seconds=self._estimate_duration(full_text),
                        target_duration_seconds=self._settings.script_target_seconds,
                        writer_name=self._writer.name,
                        review_reasons=review_reasons,
                    )
                    session.add(script)
                    run.items_created += 1
                    if review_reasons:
                        run.details = {
                            **run.details,
                            "needs_review": run.details.get("needs_review", 0) + 1,
                        }
                    else:
                        run.details = {**run.details, "drafts": run.details.get("drafts", 0) + 1}
                run.status = "completed"
            except Exception as exc:
                logger.exception("Script writer run failed")
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise RuntimeError(run.errors[-1])
        return ScriptRunRead.model_validate(run)

    async def _load_eligible_articles(
        self, session: AsyncSession
    ) -> list[tuple[Article, CredibilityAssessment, ResearchBrief]]:
        statement = (
            select(Article, CredibilityAssessment, ResearchBrief)
            .join(CredibilityAssessment, CredibilityAssessment.article_id == Article.id)
            .join(ResearchBrief, ResearchBrief.article_id == Article.id)
            .outerjoin(NewsScript, NewsScript.article_id == Article.id)
            .where(
                CredibilityAssessment.status == "corroborated",
                ResearchBrief.status == "completed",
                NewsScript.id.is_(None),
            )
            .order_by(Article.collected_at.asc())
            .limit(self._settings.script_writer_batch_size)
        )
        return list((await session.execute(statement)).tuples().all())

    def _review_reasons(self, draft: ScriptDraft, brief: ResearchBrief) -> list[str]:
        reasons: list[str] = []
        if brief.source_count < self._settings.script_minimum_research_sources:
            reasons.append("Insufficient additional research sources for automatic draft routing.")
        content = self._render(draft).lower()
        prohibited = sorted(term for term in SENSATIONAL_TERMS if term in content)
        if prohibited:
            reasons.append(f"Potentially sensational language found: {', '.join(prohibited)}.")
        if not draft.main_story:
            reasons.append("The script contains no evidence-backed main-story section.")
        return reasons

    @staticmethod
    def _render(draft: ScriptDraft) -> str:
        parts = [
            draft.hook,
            draft.introduction,
            *draft.main_story,
            *draft.context,
            draft.closing,
        ]
        return "\n\n".join(" ".join(part.split()) for part in parts if part.strip())

    @staticmethod
    def _estimate_duration(text: str) -> int:
        word_count = len(re.findall(r"\b\w+\b", text))
        return max(1, round(word_count / 2.5))

    @staticmethod
    def _create_writer(settings: Settings) -> ScriptWriter:
        if settings.script_writer_provider == "extractive":
            return ExtractiveScriptWriter()
        if settings.script_writer_provider == "ollama":
            return OllamaScriptWriter(
                settings.ollama_base_url, settings.ollama_model, settings.script_target_seconds
            )
        raise ValueError("Unsupported script writer provider; use 'extractive' or 'ollama'")
