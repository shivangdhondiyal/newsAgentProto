from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import Settings
from backend.app.db.models import (
    AgentRun,
    ContentApproval,
    PublishingRecord,
    VideoRender,
)
from backend.app.publishers.contracts import PublishJob, PublishProvider
from backend.app.publishers.providers import LocalExportPublisher, YouTubePublisher
from backend.app.reviews.quality import sha256_file
from backend.app.schemas.publishing import PublishingRunRead

logger = logging.getLogger(__name__)


def build_publishers(settings: Settings) -> dict[str, PublishProvider]:
    return {
        "local": LocalExportPublisher(settings.publishing_output_dir),
        "youtube": YouTubePublisher(
            client_id=settings.publishing_youtube_client_id,
            client_secret=settings.publishing_youtube_client_secret,
            refresh_token=settings.publishing_youtube_refresh_token,
            privacy_status=settings.publishing_youtube_privacy_status,
            category_id=settings.publishing_youtube_category_id,
            default_language=settings.publishing_youtube_default_language,
            made_for_kids=settings.publishing_youtube_made_for_kids,
        ),
    }


class PublishingAgent:
    """Publishes only explicitly approved, hash-verified content to configured providers."""

    name = "publishing"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        providers: dict[str, PublishProvider] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._providers = providers or build_publishers(settings)

    @property
    def supported_platforms(self) -> set[str]:
        return set(self._providers)

    def provider_for(self, platform: str) -> PublishProvider | None:
        return self._providers.get(platform)

    async def execute(self) -> PublishingRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(
            agent_name=self.name,
            status="running",
            started_at=started_at,
            details={"published": 0, "exported": 0, "blocked": 0, "failed": 0},
        )
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                records = await self._load_queued(session)
                run.items_seen = len(records)
                for record in records:
                    await self._publish_record(session, record, run)
                    await session.commit()
                run.status = "completed" if not run.errors else "completed_with_errors"
            except Exception as exc:
                logger.exception("Publishing run failed")
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise RuntimeError(run.errors[-1])
        return PublishingRunRead.model_validate(run)

    async def _load_queued(self, session: AsyncSession) -> list[PublishingRecord]:
        statement = (
            select(PublishingRecord)
            .where(PublishingRecord.status == "queued")
            .order_by(PublishingRecord.created_at.asc())
            .limit(self._settings.publishing_batch_size)
        )
        return list((await session.scalars(statement)).all())

    async def _publish_record(
        self, session: AsyncSession, record: PublishingRecord, run: AgentRun
    ) -> None:
        render = await session.get(VideoRender, record.video_render_id)
        approval = await session.get(ContentApproval, record.approval_id)
        if render is None or approval is None:
            self._block(record, run, "The approved video or approval record no longer exists")
            return
        if approval.status != "approved" or render.status != "approved":
            self._block(record, run, "Human approval is required before publishing")
            return
        current_hash = sha256_file(Path(render.video_path))
        if not approval.artifact_sha256 or current_hash != approval.artifact_sha256:
            self._block(
                record,
                run,
                "The video artifact changed after approval; a new review is required",
            )
            return
        pinned_hash = (record.metadata_json or {}).get("approved_artifact_sha256")
        if not isinstance(pinned_hash, str) or current_hash != pinned_hash:
            self._block(
                record,
                run,
                (
                    "The video artifact no longer matches the hash pinned when it was "
                    "queued; review and queue it again"
                ),
            )
            return
        provider = self._providers.get(record.platform)
        if provider is None:
            self._block(record, run, f"Unsupported publishing platform: {record.platform}")
            return

        record.status = "publishing"
        record.attempts += 1
        record.last_error = None
        # Persist the attempt before the external side effect so a process crash leaves
        # an explicit in-flight record rather than an automatically retryable upload.
        await session.commit()
        try:
            result = await provider.publish(
                PublishJob(
                    record_id=record.id,
                    video_render_id=record.video_render_id,
                    platform=record.platform,
                    title=record.title,
                    description=record.description,
                    tags=record.tags,
                    video_path=Path(render.video_path),
                    duration_seconds=render.duration_seconds,
                    metadata=record.metadata_json,
                )
            )
        except Exception as exc:
            logger.exception("Publishing failed", extra={"record_id": record.id})
            record.status = "failed"
            record.last_error = str(exc)[:2_000]
            run.errors = [*run.errors, f"{record.platform}: {record.last_error}"]
            run.details = {**run.details, "failed": run.details.get("failed", 0) + 1}
            return

        if result.status not in {"published", "exported"}:
            record.status = "failed"
            record.last_error = f"Provider returned unsupported status: {result.status}"
            run.errors = [*run.errors, f"{record.platform}: {record.last_error}"]
            run.details = {**run.details, "failed": run.details.get("failed", 0) + 1}
            return

        record.status = result.status
        record.external_id = result.external_id
        record.external_url = result.external_url
        record.metadata_json = {**record.metadata_json, **(result.metadata or {})}
        record.published_at = datetime.now(UTC)
        run.items_created += 1
        run.details = {
            **run.details,
            result.status: run.details.get(result.status, 0) + 1,
        }

    @staticmethod
    def _block(record: PublishingRecord, run: AgentRun, reason: str) -> None:
        record.status = "blocked"
        record.last_error = reason
        run.errors = [*run.errors, f"{record.platform}: {reason}"]
        run.details = {**run.details, "blocked": run.details.get("blocked", 0) + 1}
