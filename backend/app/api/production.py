from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import ProductionJob, VideoVariant
from backend.app.schemas.production import (
    ProductionConfigRead,
    ProductionDefaultsUpdate,
    ProductionJobCreate,
    ProductionJobRead,
    ProductionStoryRead,
)
from backend.app.services.production import (
    ProductionConflict,
    ProductionError,
    ProductionNotFound,
    ProductionService,
    build_config,
    build_job_detail,
    list_jobs,
    list_stories,
    load_defaults,
    save_defaults,
)

router = APIRouter(prefix="/production", tags=["production"])
ASPECT_DIRS = {"16:9": "16x9", "9:16": "9x16"}
FRAME_NAME = re.compile(r"^scene-(\d{1,4})\.jpg$")


async def get_db_session(request: Request):
    async with request.app.state.session_factory() as session:
        yield session


def _service(request: Request) -> ProductionService:
    service: ProductionService | None = getattr(
        request.app.state, "production_service", None
    )
    if service is None:
        raise HTTPException(
            status_code=503, detail="Production service is not available"
        )
    return service


def _handle(error: ProductionError) -> HTTPException:
    if isinstance(error, ProductionNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ProductionConflict):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


def _summary(job: ProductionJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "current_stage": job.current_stage,
        "script_id": job.script_id,
        "article_id": job.article_id,
        "video_render_id": job.video_render_id,
        "style_key": job.style_key,
        "aspect_ratio": job.aspect_ratio,
        "requested_by": job.requested_by,
        "fingerprint": job.fingerprint,
        "config_json": job.config_json or {},
        "warnings": job.warnings or [],
        "error": job.error,
        "logs": job.logs or [],
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


@router.get("/config", response_model=ProductionConfigRead)
async def production_config(request: Request) -> dict[str, Any]:
    return build_config(request.app.state.settings)


@router.patch("/defaults")
async def update_defaults(
    payload: ProductionDefaultsUpdate, request: Request
) -> dict[str, Any]:
    updates = payload.model_dump(exclude_none=True)
    return save_defaults(request.app.state.settings, updates)


@router.get("/defaults")
async def read_defaults(request: Request) -> dict[str, Any]:
    return load_defaults(request.app.state.settings)


@router.get("/stories", response_model=list[ProductionStoryRead])
async def production_stories(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, Any]]:
    stories = await list_stories(session, request.app.state.settings)
    return stories[:limit]


@router.get("/jobs", response_model=list[ProductionJobRead])
async def production_jobs(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=12, ge=1, le=50),
) -> list[dict[str, Any]]:
    jobs = await list_jobs(session, limit)
    return [_summary(job) for job in jobs]


@router.post("/jobs", response_model=ProductionJobRead, status_code=201)
async def create_production_job(
    payload: ProductionJobCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    try:
        job = await service.create_job(payload)
    except ProductionError as error:
        raise _handle(error) from error
    fresh = await session.get(ProductionJob, job.id)
    if fresh is None:
        raise HTTPException(status_code=500, detail="Production job was not persisted")
    return await build_job_detail(session, fresh, request.app.state.settings)


@router.get("/jobs/{job_id}", response_model=ProductionJobRead)
async def production_job(
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    job = await session.get(ProductionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production job not found")
    return await build_job_detail(session, job, request.app.state.settings)


@router.post("/jobs/{job_id}/start", response_model=ProductionJobRead, status_code=202)
async def start_production_job(
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    try:
        await service.start_job(job_id)
    except ProductionError as error:
        raise _handle(error) from error
    job = await session.get(ProductionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production job not found")
    return await build_job_detail(session, job, request.app.state.settings)


@router.post("/jobs/{job_id}/retry", response_model=ProductionJobRead, status_code=202)
async def retry_production_job(
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    try:
        await service.retry_job(job_id)
    except ProductionError as error:
        raise _handle(error) from error
    job = await session.get(ProductionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production job not found")
    return await build_job_detail(session, job, request.app.state.settings)


@router.get("/jobs/{job_id}/variants/{aspect_ratio}/file", include_in_schema=False)
async def stream_variant(
    job_id: str,
    aspect_ratio: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FileResponse:
    job = await session.get(ProductionJob, job_id)
    if job is None or job.video_render_id is None:
        raise HTTPException(status_code=404, detail="Production output not found")
    variant = await session.scalar(
        select(VideoVariant).where(
            VideoVariant.video_render_id == job.video_render_id,
            VideoVariant.aspect_ratio == aspect_ratio,
        )
    )
    if variant is None:
        raise HTTPException(status_code=404, detail="Variant not found")
    path = Path(variant.video_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Variant file is missing")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{job.id}-{aspect_ratio.replace(':', 'x')}.mp4",
    )


@router.get("/jobs/{job_id}/frames/{aspect_ratio}/{frame_name}", include_in_schema=False)
async def stream_frame(
    job_id: str,
    aspect_ratio: str,
    frame_name: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FileResponse:
    directory = ASPECT_DIRS.get(aspect_ratio)
    match = FRAME_NAME.match(frame_name)
    if directory is None or match is None:
        raise HTTPException(status_code=404, detail="Frame not found")
    job = await session.get(ProductionJob, job_id)
    if job is None or job.video_render_id is None:
        raise HTTPException(status_code=404, detail="Frame not found")
    variant = await session.scalar(
        select(VideoVariant).where(
            VideoVariant.video_render_id == job.video_render_id,
            VideoVariant.aspect_ratio == aspect_ratio,
        )
    )
    if variant is None:
        raise HTTPException(status_code=404, detail="Frame not found")
    frame = Path(variant.video_path).parent / "frames" / directory / frame_name
    if not frame.is_file():
        raise HTTPException(status_code=404, detail="Frame file is missing")
    return FileResponse(frame, media_type="image/jpeg")
