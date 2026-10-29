from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.app.agents.errors import AgentRunFailed
from backend.app.services.run_control import RunInProgressError
from backend.app.services.workflow_dispatch import (
    WorkflowDispatcher,
    WorkflowStageNotFound,
)

router = APIRouter(prefix="/workflow", tags=["workflow"])
T = TypeVar("T")


class WorkflowTriggerRequest(BaseModel):
    stage: str = Field(min_length=1, max_length=50)


class WorkflowTriggerRead(BaseModel):
    accepted: bool
    stage: str
    label: str
    agent_name: str
    accepted_at: datetime
    poll_path: str


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _error_detail(
    request: Request,
    *,
    code: str,
    message: str,
    agent_name: str,
    run_id: str | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "agent": agent_name,
        "run_id": run_id,
        "errors": list(errors or []),
        "request_id": _request_id(request),
    }


async def execute_synchronously(
    request: Request,
    *,
    agent_name: str,
    label: str,
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Execute a legacy synchronous workflow endpoint with structured failures."""

    coordinator = getattr(request.app.state, "run_coordinator", None)
    try:
        if coordinator is None:
            return await operation()
        result = await coordinator.run(
            agent_name,
            operation,
            reject_if_busy=True,
            label=label,
        )
        return result  # type: ignore[return-value]
    except RunInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail=_error_detail(
                request,
                code="run_in_progress",
                message=(
                    f"{label} is already running. Wait for it to finish or check "
                    "the agent runs before retrying."
                ),
                agent_name=agent_name,
            ),
        ) from exc
    except AgentRunFailed as exc:
        raise HTTPException(
            status_code=502,
            detail=_error_detail(
                request,
                code="agent_run_failed",
                message=f"{label} failed",
                agent_name=agent_name,
                run_id=exc.run_id,
                errors=exc.errors[-3:],
            ),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=_error_detail(
                request,
                code="agent_run_failed",
                message=f"{label} failed",
                agent_name=agent_name,
                errors=[str(exc)],
            ),
        ) from exc


@router.post("/runs", status_code=202, response_model=WorkflowTriggerRead)
async def trigger_workflow(
    payload: WorkflowTriggerRequest,
    request: Request,
) -> WorkflowTriggerRead:
    dispatcher: WorkflowDispatcher | None = getattr(request.app.state, "workflow_dispatcher", None)
    if dispatcher is None:
        raise HTTPException(
            status_code=503,
            detail=_error_detail(
                request,
                code="workflow_dispatcher_unavailable",
                message="The workflow dispatcher is not available.",
                agent_name="workflow",
            ),
        )
    try:
        result = dispatcher.trigger(payload.stage)
    except WorkflowStageNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=_error_detail(
                request,
                code="workflow_stage_not_found",
                message=f"Unknown workflow stage: {payload.stage}",
                agent_name="workflow",
            ),
        ) from exc
    except RunInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail=_error_detail(
                request,
                code="run_in_progress",
                message=(
                    f"{exc.label} is already running. Wait for it to finish or "
                    "check the agent runs before retrying."
                ),
                agent_name=exc.agent_name,
            ),
        ) from exc
    return WorkflowTriggerRead(
        accepted=True,
        stage=result.stage,
        label=result.label,
        agent_name=result.agent_name,
        accepted_at=result.accepted_at,
        poll_path=result.poll_path,
    )
