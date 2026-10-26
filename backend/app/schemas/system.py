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


class OverviewMetricRead(BaseModel):
    key: str
    label: str
    value: int
    detail: str


class PipelineStageRead(BaseModel):
    key: str
    label: str
    description: str
    agent_name: str
    status: str
    status_label: str
    count: int
    count_label: str
    action: str
    action_path: str
    last_run: AgentRunRead | None = None


class AttentionItemRead(BaseModel):
    id: str
    kind: str
    severity: str
    title: str
    detail: str
    href: str | None = None
    created_at: datetime | None = None


class ServiceHealthRead(BaseModel):
    key: str
    label: str
    ready: bool
    required: bool
    detail: str


class OverviewRead(BaseModel):
    generated_at: datetime
    status: str
    scheduler_running: bool
    last_successful_sync: datetime | None
    active_runs: int
    metrics: list[OverviewMetricRead]
    pipeline: list[PipelineStageRead]
    attention: list[AttentionItemRead]
    recent_runs: list[AgentRunRead]
    services: list[ServiceHealthRead]
    journal: dict[str, int]
