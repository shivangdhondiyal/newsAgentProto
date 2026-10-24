from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None
    items_seen: int
    items_created: int
    items_updated: int
    warnings: list[str]
    errors: list[str]
    details: dict[str, Any]


class ApprovalEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_render_id: str | None
    script_id: str | None
    action: str
    actor: str
    notes: str | None
    created_at: datetime


class SystemStatusRead(BaseModel):
    status: str
    scheduler_running: bool
    journal: dict[str, int]
    capabilities: dict[str, bool]
