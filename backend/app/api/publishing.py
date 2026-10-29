from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from urllib.request import url2pathname

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.serializers import as_utc
from backend.app.api.workflow import execute_synchronously
from backend.app.core.config import get_settings
from backend.app.db.models import (
    Article,
    ContentApproval,
    NarrationAudio,
    NewsScript,
    PublishingRecord,
    ResearchSource,
    Scene,
    ScenePlan,
    VideoRender,
)
from backend.app.reviews.quality import sha256_file
from backend.app.schemas.publishing import (
    PublishingAnalyticsRead,
    PublishingQueueRequest,
    PublishingRecordRead,
    PublishingRunRead,
)

router = APIRouter(prefix="/publishing", tags=["publishing"])


async def get_db_session(request: Request):
    async with request.app.state.session_factory() as session:
        yield session


@router.post("/queue", response_model=PublishingRecordRead, status_code=201)
async def queue_publish(
    request: Request,
    payload: PublishingQueueRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PublishingRecord:
    row = (
        (
            await session.execute(
                select(VideoRender, NarrationAudio, NewsScript, Article)
                .join(NarrationAudio, NarrationAudio.id == VideoRender.narration_audio_id)
                .join(NewsScript, NewsScript.id == NarrationAudio.script_id)
                .join(Article, Article.id == NewsScript.article_id)
                .where(VideoRender.id == payload.video_render_id)
            )
        )
        .tuples()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Video render not found")
    render, _narration, script, article = row
    approval = await session.scalar(
        select(ContentApproval).where(ContentApproval.video_render_id == render.id)
    )
    if approval is None or approval.status != "approved" or render.status != "approved":
        raise HTTPException(
            status_code=409,
            detail="Explicit human approval is required before content can be queued",
        )
    current_hash = sha256_file(Path(render.video_path))
    if current_hash is None or current_hash != approval.artifact_sha256:
        raise HTTPException(
            status_code=409,
            detail="The video changed after approval; run a new review before publishing",
        )

    platform = payload.platform or get_settings().publishing_default_platform
    agent = request.app.state.publishing_agent
    provider = agent.provider_for(platform)
    if provider is None:
        available = sorted(agent.supported_platforms)
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported publishing platform. Available: {available}",
        )
    if getattr(provider, "configured", True) is False:
        raise HTTPException(
            status_code=409,
            detail=f"Platform '{platform}' is not configured; add its credentials to .env",
        )

    existing = await session.scalar(
        select(PublishingRecord).where(
            PublishingRecord.video_render_id == render.id,
            PublishingRecord.platform == platform,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "This video is already queued or has a publishing record for this platform"
                ),
                "record_id": existing.id,
                "status": existing.status,
            },
        )

    research_sources = list(
        (
            await session.scalars(
                select(ResearchSource).where(ResearchSource.brief_id == script.research_brief_id)
            )
        ).all()
    )
    scene_keywords = list(
        (
            await session.scalars(
                select(Scene.keywords)
                .join(ScenePlan, ScenePlan.id == Scene.scene_plan_id)
                .where(ScenePlan.script_id == script.id)
            )
        ).all()
    )
    tags = _build_tags(article, scene_keywords, payload.tags)
    title = " ".join((payload.title or script.title).split())[:100]
    description = payload.description or _build_description(script, research_sources)

    record = PublishingRecord(
        video_render_id=render.id,
        approval_id=approval.id,
        platform=platform,
        status="queued",
        title=title,
        description=description[:5_000],
        tags=tags,
        idempotency_key=f"{render.id}:{platform}",
        metadata_json={
            "video_path": render.video_path,
            "script_id": script.id,
            "article_id": article.id,
            "duration_seconds": render.duration_seconds,
            "approved_artifact_sha256": approval.artifact_sha256,
            "queued_at": datetime.now(UTC).isoformat(),
        },
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="A publishing record already exists for this video/platform"
        ) from exc
    await session.refresh(record)
    return record


@router.post("/runs", response_model=PublishingRunRead)
async def run_publishing(request: Request) -> PublishingRunRead:
    return await execute_synchronously(
        request,
        agent_name="publishing",
        label="Process publishing",
        operation=request.app.state.publishing_agent.execute,
    )


@router.get("/analytics", response_model=PublishingAnalyticsRead)
async def publishing_analytics(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    days: int = Query(default=30, ge=1, le=365),
) -> PublishingAnalyticsRead:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    records = list(
        (
            await session.scalars(
                select(PublishingRecord).order_by(PublishingRecord.created_at.desc())
            )
        ).all()
    )
    recent = [record for record in records if (as_utc(record.created_at) or cutoff) >= cutoff]
    by_status: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    for record in recent:
        by_status[record.status] = by_status.get(record.status, 0) + 1
        by_platform[record.platform] = by_platform.get(record.platform, 0) + 1
    successful = by_status.get("published", 0) + by_status.get("exported", 0)
    attempted = successful + by_status.get("failed", 0) + by_status.get("blocked", 0)
    success_rate = round(successful / attempted * 100, 1) if attempted else 0.0
    approval_counts = {
        status: count
        for status, count in (
            await session.execute(
                select(ContentApproval.status, func.count()).group_by(ContentApproval.status)
            )
        ).all()
    }
    return PublishingAnalyticsRead(
        days=days,
        by_status=by_status,
        by_platform=by_platform,
        approved_videos=approval_counts.get("approved", 0),
        pending_reviews=approval_counts.get("pending", 0),
        publishing_success_rate=success_rate,
        external_publish_count=sum(
            1 for record in recent if record.status == "published" and record.platform != "local"
        ),
        recent_records=[PublishingRecordRead.model_validate(record) for record in recent[:10]],
    )


@router.get("/records", response_model=list[PublishingRecordRead])
async def list_publishing_records(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    status: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    video_render_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[PublishingRecord]:
    statement = select(PublishingRecord).order_by(PublishingRecord.created_at.desc()).limit(limit)
    if status:
        statement = statement.where(PublishingRecord.status == status)
    if platform:
        statement = statement.where(PublishingRecord.platform == platform)
    if video_render_id:
        statement = statement.where(PublishingRecord.video_render_id == video_render_id)
    return list((await session.scalars(statement)).all())


@router.get("/records/{record_id}/file")
async def download_local_export(
    request: Request,
    record_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FileResponse:
    record = await session.get(PublishingRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Publishing record not found")
    if record.status not in {"exported", "published"}:
        raise HTTPException(status_code=409, detail="This record has no completed export")
    if record.platform != "local" or not (record.external_url or "").startswith("file://"):
        raise HTTPException(status_code=404, detail="This record has no local export file")
    settings = getattr(request.app.state, "settings", None) or get_settings()
    published_root = Path(settings.publishing_output_dir).resolve()
    try:
        export_path = Path(url2pathname(urlparse(record.external_url).path)).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Export file is unavailable") from None
    if published_root not in export_path.parents or not export_path.is_file():
        raise HTTPException(status_code=404, detail="Export file is unavailable")
    return FileResponse(export_path, filename=export_path.name, media_type="video/mp4")


@router.post("/records/{record_id}/retry", response_model=PublishingRecordRead)
async def retry_publishing_record(
    record_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PublishingRecord:
    record = await session.get(PublishingRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Publishing record not found")
    if record.status not in {"failed", "blocked"}:
        raise HTTPException(
            status_code=409,
            detail="Only failed or blocked publishing records can be retried",
        )
    record.status = "queued"
    record.last_error = None
    await session.commit()
    await session.refresh(record)
    return record


def _build_tags(
    article: Article, scene_keywords: list[list[str]], requested: list[str]
) -> list[str]:
    tags: list[str] = []
    for value in [
        *requested,
        *(article.category or "").split(),
        *(article.country or "").split(),
        *(keyword for group in scene_keywords for keyword in group),
    ]:
        cleaned = " ".join(str(value).split()).strip()
        if cleaned and cleaned.lower() not in {tag.lower() for tag in tags}:
            tags.append(cleaned[:100])
    return tags[:30]


def _build_description(script: NewsScript, sources: list[ResearchSource]) -> str:
    parts = [script.hook, script.introduction]
    if script.main_story:
        parts.append(script.main_story[0])
    description = "\n\n".join(" ".join(part.split()) for part in parts if part.strip())
    if sources:
        citation_lines = [
            f"- {source.publisher}: {source.url}" for source in sources[:10] if source.url
        ]
        if citation_lines:
            description += "\n\nSources:\n" + "\n".join(citation_lines)
    description += (
        "\n\nHuman-reviewed draft. This description preserves the sources used in the "
        "research brief and does not add unverified claims."
    )
    return description[:5_000]
