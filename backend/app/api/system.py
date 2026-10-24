from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.db.models import (
    AgentRun,
    ApprovalEvent,
    Article,
    ContentApproval,
    NewsScript,
    PublishingRecord,
    VideoRender,
)
from backend.app.schemas.system import (
    AgentRunRead,
    ApprovalEventRead,
    SystemStatusRead,
)

router = APIRouter(tags=["system"])


async def get_db_session(request: Request):
    async with request.app.state.session_factory() as session:
        yield session


@router.get("/agent-runs", response_model=list[AgentRunRead])
async def list_agent_runs(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    agent_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AgentRun]:
    statement = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
    if agent_name:
        statement = statement.where(AgentRun.agent_name == agent_name)
    if status:
        statement = statement.where(AgentRun.status == status)
    return list((await session.scalars(statement)).all())


@router.get("/approval-events", response_model=list[ApprovalEventRead])
async def list_approval_events(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    video_render_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ApprovalEvent]:
    statement = select(ApprovalEvent).order_by(ApprovalEvent.created_at.desc()).limit(limit)
    if video_render_id:
        statement = statement.where(ApprovalEvent.video_render_id == video_render_id)
    return list((await session.scalars(statement)).all())


@router.get("/system/status", response_model=SystemStatusRead)
async def system_status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SystemStatusRead:
    async def count(model: object) -> int:
        return int((await session.scalar(select(func.count()).select_from(model))) or 0)

    scheduler = getattr(request.app.state, "scheduler", None)
    settings = getattr(request.app.state, "settings", None) or get_settings()
    capabilities = {
        "video_ffmpeg": _command_available(settings.video_ffmpeg_executable),
        "tts_ffmpeg": _command_available(settings.tts_ffmpeg_executable),
        "piper_tts": _command_available(settings.tts_piper_executable)
        and Path(settings.tts_piper_voice_model).is_file(),
        "local_embeddings": importlib.util.find_spec("sentence_transformers") is not None,
        "local_publishing": True,
        "youtube_publishing": all(
            (
                settings.publishing_youtube_client_id,
                settings.publishing_youtube_client_secret,
                settings.publishing_youtube_refresh_token,
            )
        ),
    }
    return SystemStatusRead(
        status="ok",
        scheduler_running=bool(scheduler and scheduler.running),
        capabilities=capabilities,
        journal={
            "articles": await count(Article),
            "scripts": await count(NewsScript),
            "video_renders": await count(VideoRender),
            "pending_approvals": int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(ContentApproval)
                        .where(ContentApproval.status == "pending")
                    )
                )
                or 0
            ),
            "publishing_records": await count(PublishingRecord),
        },
    )


def _command_available(command: str) -> bool:
    if not command:
        return False
    return shutil.which(command) is not None or Path(command).is_file()
