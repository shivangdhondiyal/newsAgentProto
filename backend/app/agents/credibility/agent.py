from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.credibility.contracts import (
    ClaimExtractor,
    EvidenceCandidate,
    EvidenceFinder,
)
from backend.app.agents.credibility.providers import (
    HeuristicClaimExtractor,
    LocalEvidenceFinder,
    OllamaClaimExtractor,
)
from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.agents.errors import AgentRunFailed
from backend.app.agents.independence import publisher_identity
from backend.app.core.config import Settings
from backend.app.db.models import AgentRun, Article, CredibilityAssessment, CredibilityEvidence
from backend.app.schemas.credibility import CredibilityRunRead

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoreResult:
    score: float
    status: str
    independent_sources: int
    explanation: str


class CredibilityAgent:
    """Scores corroboration evidence and deliberately routes uncertainty to review."""

    name = "credibility"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        claim_extractor: ClaimExtractor | None = None,
        evidence_finder: EvidenceFinder | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._trust_policy = SourceTrustPolicy.from_file(settings.trust_policy_file)
        self._claim_extractor = claim_extractor or self._create_extractor(settings)
        self._evidence_finder = evidence_finder or LocalEvidenceFinder(
            session_factory, self._trust_policy
        )

    async def execute(self) -> CredibilityRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                articles = await self._load_unassessed_articles(session)
                run.items_seen = len(articles)
                for article in articles:
                    assessment, evidence_by_claim = await self._assess(article)
                    session.add(assessment)
                    await session.flush()
                    for claim, evidence in evidence_by_claim:
                        session.add(
                            CredibilityEvidence(
                                assessment_id=assessment.id,
                                claim=claim,
                                source_id=evidence.source_id,
                                publisher=evidence.publisher,
                                url=evidence.url,
                                title=evidence.title,
                                snippet=evidence.snippet,
                                relevance_score=evidence.relevance_score,
                                trust_weight=self._trust_policy.weight_for(evidence.source_id),
                            )
                        )
                    run.items_created += 1
                    if assessment.status == "corroborated":
                        run.details = {
                            **run.details,
                            "corroborated": run.details.get("corroborated", 0) + 1,
                        }
                    else:
                        run.details = {
                            **run.details,
                            "needs_review": run.details.get("needs_review", 0) + 1,
                        }
                run.status = "completed"
            except Exception as exc:
                logger.exception("Credibility run failed")
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise AgentRunFailed(self.name, run.id, run.errors[-1], run.errors)
        return CredibilityRunRead.model_validate(run)

    async def _load_unassessed_articles(self, session: AsyncSession) -> list[Article]:
        statement = (
            select(Article)
            .outerjoin(CredibilityAssessment, CredibilityAssessment.article_id == Article.id)
            .where(CredibilityAssessment.id.is_(None))
            .order_by(Article.collected_at.asc())
            .limit(self._settings.credibility_batch_size)
        )
        return list((await session.scalars(statement)).all())

    async def _assess(
        self, article: Article
    ) -> tuple[CredibilityAssessment, list[tuple[str, EvidenceCandidate]]]:
        claims = await self._claim_extractor.extract(article)
        evidence_by_claim: list[tuple[str, EvidenceCandidate]] = []
        for claim in claims:
            findings = await self._evidence_finder.find(claim, article)
            evidence_by_claim.extend((claim, finding) for finding in findings)
        score_result = self._score(article, [finding for _, finding in evidence_by_claim])
        assessment = CredibilityAssessment(
            article_id=article.id,
            status=score_result.status,
            confidence_score=score_result.score,
            explanation=score_result.explanation,
            claims=claims,
            independent_source_count=score_result.independent_sources,
            extractor_name=self._claim_extractor.name,
        )
        return assessment, evidence_by_claim

    def _score(self, article: Article, evidence: list[EvidenceCandidate]) -> ScoreResult:
        target_identity = publisher_identity(
            article.source_id,
            article.source_name,
            article.url,
            article.canonical_url,
            self._trust_policy,
        )
        strongest_by_publisher: dict[str, tuple[EvidenceCandidate, float]] = {}
        for item in evidence:
            identity = publisher_identity(
                item.source_id,
                item.publisher,
                item.url,
                trust_policy=self._trust_policy,
            )
            if identity == target_identity:
                continue
            weighted = item.relevance_score * self._trust_policy.weight_for(item.source_id)
            current = strongest_by_publisher.get(identity)
            if current is None or weighted > current[1]:
                strongest_by_publisher[identity] = (item, weighted)
        strongest = sorted(strongest_by_publisher.values(), key=lambda item: item[1], reverse=True)[
            :3
        ]
        source_count = len(strongest)
        target_weight = self._trust_policy.weight_for(article.source_id)
        evidence_strength = (
            sum(weight for _, weight in strongest) / source_count if strongest else 0.0
        )
        diversity = min(source_count / 3, 1.0)
        score = min(0.95, 0.10 + 0.15 * target_weight + 0.50 * evidence_strength + 0.25 * diversity)
        if source_count < 2:
            score = min(score, 0.65)
            status = "needs_review"
            explanation = (
                "Fewer than two independent trusted sources produced relevant corroboration; "
                "manual review is required."
            )
        elif score >= self._settings.credibility_confidence_threshold:
            status = "corroborated"
            explanation = (
                f"{source_count} independent trusted sources produced relevant corroboration. "
                "This is an evidence-based confidence estimate, not proof of fact."
            )
        else:
            status = "needs_review"
            explanation = (
                "Multiple sources were found, but their trust-weighted relevance was below "
                "the configured confidence threshold."
            )
        return ScoreResult(round(score, 3), status, source_count, explanation)

    @staticmethod
    def _create_extractor(settings: Settings) -> ClaimExtractor:
        if settings.claim_extraction_provider == "heuristic":
            return HeuristicClaimExtractor()
        if settings.claim_extraction_provider == "ollama":
            return OllamaClaimExtractor(settings.ollama_base_url, settings.ollama_model)
        raise ValueError("Unsupported claim extraction provider; use 'heuristic' or 'ollama'")
