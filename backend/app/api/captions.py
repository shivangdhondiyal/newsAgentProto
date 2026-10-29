from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import NarrationAudio, ProductionJob
from backend.app.schemas.captions import (
    CaptionConfigRead,
    CaptionCuesUpdate,
    CaptionImportRequest,
    CaptionJobRead,
    CaptionRegenerateRequest,
    CaptionSettingsUpdate,
    CaptionTrackRead,
)
from backend.app.services.captions import (
    CaptionConflict,
    CaptionError,
    CaptionNotFound,
    CaptionService,
)
from backend.app.services.production import (
    ProductionConflict,
    ProductionError,
    ProductionNotFound,
    ProductionService,
    build_job_detail,
)

router = APIRouter(prefix="/captions", tags=["captions"])

MEDIA_TYPES = {
    "srt": "application/x-subrip; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "ass": "text/x-ssa; charset=utf-8",
}


async def get_db_session(request: Request):
    async with request.app.state.session_factory() as session:
        yield session


def _service(request: Request) -> CaptionService:
    service: CaptionService | None = getattr(request.app.state, "caption_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Caption service is not available")
    return service


def _production(request: Request) -> ProductionService:
    service: ProductionService | None = getattr(request.app.state, "production_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Production service is not available")
    return service


def _handle(error: CaptionError) -> HTTPException:
    if isinstance(error, CaptionNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, CaptionConflict):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


async def _duration(session: AsyncSession, track) -> float | None:
    if not track.narration_audio_id:
        return None
    narration = await session.get(NarrationAudio, track.narration_audio_id)
    return float(narration.duration_seconds) if narration and narration.duration_seconds else None


@router.get("/config", response_model=CaptionConfigRead)
async def caption_config(request: Request) -> dict[str, Any]:
    return _service(request).build_config()


@router.get("/jobs/{job_id}", response_model=CaptionJobRead)
async def caption_job(
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    try:
        job, narration, track = await service.get_for_job(session, job_id)
    except CaptionError as error:
        raise _handle(error) from error
    payload: dict[str, Any] = {
        "job_id": job.id,
        "track": None,
        "narration_audio_id": narration.id if narration else None,
        "duration_seconds": float(narration.duration_seconds)
        if narration and narration.duration_seconds
        else None,
        "can_generate": narration is not None,
        "blocking_reason": None
        if narration is not None
        else "Generate the narration before creating captions.",
        "burn_in": bool((job.config_json or {}).get("captions", True)),
        "soft_subs": request.app.state.settings.caption_soft_subs_default,
        "sidecar": request.app.state.settings.caption_sidecar_default,
    }
    if track is not None:
        payload["track"] = service.serialize_track(
            track,
            duration_seconds=float(narration.duration_seconds)
            if narration and narration.duration_seconds
            else None,
        )
    return payload


@router.get("/tracks/{track_id}", response_model=CaptionTrackRead)
async def caption_track(
    track_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    try:
        track = await service.get_track(session, track_id)
    except CaptionError as error:
        raise _handle(error) from error
    return service.serialize_track(track, duration_seconds=await _duration(session, track))


@router.post("/jobs/{job_id}/generate", response_model=CaptionTrackRead, status_code=201)
async def generate_job_captions(
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    payload: CaptionSettingsUpdate | None = None,
) -> dict[str, Any]:
    service = _service(request)
    job = await session.get(ProductionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production job not found")
    narration = (
        await session.scalar(
            select(NarrationAudio).where(NarrationAudio.script_id == job.script_id)
        )
        if job.script_id
        else None
    )
    if narration is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Narration is not ready. Generate the voiceover before creating "
                "captions so they can be aligned to the final audio."
            ),
        )
    settings = payload.settings.to_media() if payload and payload.settings else None
    try:
        track = await service.generate_for_script(
            session,
            job.script_id,
            narration_id=narration.id,
            preset_key=payload.preset_key if payload else None,
            settings=settings,
        )
    except CaptionError as error:
        raise _handle(error) from error
    return service.serialize_track(
        track, duration_seconds=float(narration.duration_seconds or 0.0) or None
    )


@router.post("/tracks/{track_id}/regenerate", response_model=CaptionTrackRead)
async def regenerate_track(
    track_id: str,
    payload: CaptionRegenerateRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    settings = payload.settings.to_media() if payload.settings else None
    try:
        track = await service.regenerate(
            session,
            track_id,
            force=payload.force,
            preset_key=payload.preset_key,
            settings=settings,
        )
    except CaptionError as error:
        raise _handle(error) from error
    return service.serialize_track(track, duration_seconds=await _duration(session, track))


@router.patch("/tracks/{track_id}/settings", response_model=CaptionTrackRead)
async def update_track_settings(
    track_id: str,
    payload: CaptionSettingsUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    settings = payload.settings.to_media() if payload.settings else None
    try:
        track = await service.update_settings(
            session,
            track_id,
            preset_key=payload.preset_key,
            settings=settings,
        )
    except CaptionError as error:
        raise _handle(error) from error
    return service.serialize_track(track, duration_seconds=await _duration(session, track))


@router.put("/tracks/{track_id}/cues", response_model=CaptionTrackRead)
async def update_track_cues(
    track_id: str,
    payload: CaptionCuesUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    settings = payload.settings.to_media() if payload.settings else None
    try:
        track = await service.update_cues(
            session,
            track_id,
            [cue.model_dump() for cue in payload.cues],
            settings=settings,
        )
    except CaptionError as error:
        raise _handle(error) from error
    return service.serialize_track(track, duration_seconds=await _duration(session, track))


@router.post("/tracks/{track_id}/import", response_model=CaptionTrackRead)
async def import_subtitles(
    track_id: str,
    payload: CaptionImportRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    settings = payload.settings.to_media() if payload.settings else None
    try:
        track = await service.import_subtitles(
            session,
            track_id,
            content=payload.content,
            format=payload.format,
            settings=settings,
        )
    except CaptionError as error:
        raise _handle(error) from error
    return service.serialize_track(track, duration_seconds=await _duration(session, track))


@router.get("/tracks/{track_id}/export/{fmt}", include_in_schema=False)
async def export_captions(
    track_id: str,
    fmt: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    aspect: str = Query(default="16:9", pattern=r"^(16:9|9:16)$"),
) -> FileResponse:
    service = _service(request)
    try:
        track = await service.get_track(session, track_id)
    except CaptionError as error:
        raise _handle(error) from error
    assets = await service.write_assets(track)
    normalized = fmt.lower()
    if normalized in {"srt", "vtt"}:
        path = assets.srt_path if normalized == "srt" else assets.vtt_path
        media_type = MEDIA_TYPES[normalized]
    elif normalized == "ass":
        path = assets.ass_paths.get(aspect)
        media_type = MEDIA_TYPES["ass"]
    else:
        raise HTTPException(status_code=404, detail="Unsupported caption format")
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Caption file is not available")
    suffix = f"-{aspect.replace(':', 'x')}" if normalized == "ass" else ""
    return FileResponse(
        path,
        media_type=media_type,
        filename=f"captions{suffix}.{normalized}",
    )


@router.post("/jobs/{job_id}/rerender", status_code=202)
async def rerender_with_captions(
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    production = _production(request)
    try:
        await production.rerender_job(job_id)
    except ProductionError as error:
        if isinstance(error, ProductionNotFound):
            raise HTTPException(status_code=404, detail=str(error)) from error
        if isinstance(error, ProductionConflict):
            raise HTTPException(status_code=409, detail=str(error)) from error
        raise HTTPException(status_code=422, detail=str(error)) from error
    job = await session.get(ProductionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production job not found")
    return await build_job_detail(session, job, request.app.state.settings)
