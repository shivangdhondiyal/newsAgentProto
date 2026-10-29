from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import NewsScript, ProductionJob, VideoVariant
from backend.app.media.audio_production import (
    VoiceDelivery,
    list_voices,
    synthesize_voice_preview,
)
from backend.app.media.genres import list_genres, recommend_genre
from backend.app.schemas.production import (
    GraphicSpecUpdate,
    ProductionConfigRead,
    ProductionDefaultsUpdate,
    ProductionJobCreate,
    ProductionJobRead,
    ProductionJobSettingsUpdate,
    ProductionStoryRead,
    VoicePreviewRequest,
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
    service: ProductionService | None = getattr(request.app.state, "production_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Production service is not available")
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
        "genre_key": job.genre_key,
        "genre_name": (job.genre_profile_json or {}).get("genre_name"),
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


@router.get("/genres")
async def production_genres(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    script_id: str | None = Query(default=None),
    article_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """List every genre plus an Auto recommendation for the chosen story."""

    text = ""
    if script_id:
        script = await session.get(NewsScript, script_id)
        if script is not None:
            text = " ".join(part for part in (script.title, script.full_text) if part)
    elif article_id:
        script = await session.scalar(
            select(NewsScript).where(NewsScript.article_id == article_id).limit(1)
        )
        if script is not None:
            text = " ".join(part for part in (script.title, script.full_text) if part)
    recommendation = recommend_genre(text)
    return {
        "genres": list_genres(),
        "auto": {
            "genre_key": recommendation.genre.key,
            "genre_name": recommendation.genre.name,
            "rationale": recommendation.rationale,
        },
        "voices": list_voices(request.app.state.settings),
    }


@router.post("/voices/preview", include_in_schema=False)
async def preview_voice(payload: VoicePreviewRequest, request: Request) -> FileResponse:
    settings = request.app.state.settings
    text = " ".join((payload.text or "").split())
    if not text:
        text = "This is a short narration preview for the selected news genre."
    delivery = VoiceDelivery(
        rate=payload.rate or 1.0,
        expressiveness=payload.expressiveness or 0.667,
    )
    output = Path(settings.tts_output_dir) / "previews" / "voice-preview.wav"
    try:
        await synthesize_voice_preview(
            settings,
            text=text,
            output_path=output,
            voice_key=payload.voice,
            delivery=delivery,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return FileResponse(output, media_type="audio/wav", filename="voice-preview.wav")


@router.patch("/defaults")
async def update_defaults(payload: ProductionDefaultsUpdate, request: Request) -> dict[str, Any]:
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


@router.post("/jobs/{job_id}/rerender", response_model=ProductionJobRead, status_code=202)
async def rerender_production_job(
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    """Re-run render + verify without repeating prepare/scenes/assets/audio.

    Used after caption edits, genre/settings changes, or motion-graphics edits
    so the operator never has to start a new production to see their changes.
    """

    service = _service(request)
    try:
        await service.rerender_job(job_id)
    except ProductionError as error:
        raise _handle(error) from error
    job = await session.get(ProductionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production job not found")
    return await build_job_detail(session, job, request.app.state.settings)


@router.patch("/jobs/{job_id}/settings", response_model=ProductionJobRead)
async def update_job_settings(
    job_id: str,
    payload: ProductionJobSettingsUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    service = _service(request)
    try:
        await service.update_job_settings(
            job_id,
            genre_key=payload.genre_key,
            genre_overrides=payload.genre_overrides,
            style_key=payload.style_key,
            audio_overrides=payload.audio_overrides,
            captions=payload.captions,
        )
    except ProductionError as error:
        raise _handle(error) from error
    job = await session.get(ProductionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production job not found")
    return await build_job_detail(session, job, request.app.state.settings)


@router.patch("/jobs/{job_id}/scenes/{scene_position}/graphics/{aspect_ratio}/{graphic_id}")
async def update_graphic(
    job_id: str,
    scene_position: int,
    aspect_ratio: str,
    graphic_id: str,
    payload: GraphicSpecUpdate,
    request: Request,
) -> dict[str, Any]:
    service = _service(request)
    try:
        graphic = await service.update_graphic(
            job_id,
            scene_position=scene_position,
            aspect_ratio=aspect_ratio,
            graphic_id=graphic_id,
            changes=payload.model_dump(exclude_none=True),
        )
    except ProductionError as error:
        raise _handle(error) from error
    return {"graphic": graphic}


@router.get(
    "/jobs/{job_id}/scenes/{scene_position}/graphics/{aspect_ratio}/{graphic_id}/preview",
    include_in_schema=False,
)
async def preview_graphic(
    job_id: str,
    scene_position: int,
    aspect_ratio: str,
    graphic_id: str,
    request: Request,
) -> FileResponse:
    service = _service(request)
    output = (
        Path(request.app.state.settings.video_output_dir)
        / job_id
        / "graphic-previews"
        / f"{scene_position:03d}-{graphic_id}-{aspect_ratio.replace(':', 'x')}.png"
    )
    try:
        path = await service.graphic_preview(
            job_id,
            scene_position=scene_position,
            aspect_ratio=aspect_ratio,
            graphic_id=graphic_id,
            output_path=output,
        )
    except ProductionError as error:
        raise _handle(error) from error
    return FileResponse(path, media_type="image/png")


@router.get("/jobs/{job_id}/contact-sheet/{aspect_ratio}", include_in_schema=False)
async def stream_contact_sheet(
    job_id: str,
    aspect_ratio: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FileResponse:
    job = await session.get(ProductionJob, job_id)
    if job is None or job.video_render_id is None:
        raise HTTPException(status_code=404, detail="Contact sheet not found")
    variant = await session.scalar(
        select(VideoVariant).where(
            VideoVariant.video_render_id == job.video_render_id,
            VideoVariant.aspect_ratio == aspect_ratio,
        )
    )
    if variant is None:
        raise HTTPException(status_code=404, detail="Contact sheet not found")
    video = Path(variant.video_path)
    sheet = video.with_name(video.stem + "-contact-sheet.jpg")
    if not sheet.is_file():
        raise HTTPException(status_code=404, detail="Contact sheet has not been generated")
    return FileResponse(sheet, media_type="image/jpeg")


@router.get("/jobs/{job_id}/audio/mix", include_in_schema=False)
async def stream_audio_mix(
    job_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FileResponse:
    job = await session.get(ProductionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Production job not found")
    detail = await build_job_detail(session, job, request.app.state.settings)
    audio = detail.get("audio") or {}
    mix_path = audio.get("mix_path") or audio.get("path")
    if not mix_path or not Path(mix_path).is_file():
        raise HTTPException(status_code=404, detail="Final audio mix is not available")
    return FileResponse(
        Path(mix_path),
        media_type="audio/wav",
        filename=f"{job.id}-final-mix.wav",
    )


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
