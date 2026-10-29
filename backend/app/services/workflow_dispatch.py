from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.app.services.run_control import AgentRunCoordinator, RunInProgressError

logger = logging.getLogger(__name__)


class WorkflowStageNotFound(KeyError):
    """Raised when a workflow stage key is not registered."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(stage)


@dataclass(frozen=True)
class WorkflowStage:
    key: str
    label: str
    description: str
    agent_name: str
    operation: Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class WorkflowTriggerResult:
    stage: str
    label: str
    agent_name: str
    accepted_at: datetime
    poll_path: str


class WorkflowDispatcher:
    """Runs workflow agents outside the HTTP request lifecycle.

    Long agent executions can outlive proxy or browser timeouts. The dashboard starts
    a run through a short 202 response and polls the audit trail for completion, so a
    slow feed fetch or FFmpeg render cannot turn into a misleading 502 response.
    """

    def __init__(
        self,
        coordinator: AgentRunCoordinator,
        stages: list[WorkflowStage],
    ) -> None:
        self._coordinator = coordinator
        self._stages = {stage.key: stage for stage in stages}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending_agents: set[str] = set()

    @property
    def stages(self) -> dict[str, WorkflowStage]:
        return dict(self._stages)

    def trigger(self, stage_key: str) -> WorkflowTriggerResult:
        stage = self._stages.get(stage_key)
        if stage is None:
            raise WorkflowStageNotFound(stage_key)
        if stage.agent_name in self._pending_agents or self._coordinator.is_running(
            stage.agent_name
        ):
            raise RunInProgressError(stage.agent_name, stage.label)

        accepted_at = datetime.now(UTC)
        self._pending_agents.add(stage.agent_name)
        task = asyncio.create_task(
            self._execute(stage),
            name=f"workflow:{stage.key}:{accepted_at.isoformat()}",
        )
        self._tasks.add(task)

        def _finished(_task: asyncio.Task[Any]) -> None:
            self._tasks.discard(_task)
            self._pending_agents.discard(stage.agent_name)

        task.add_done_callback(_finished)
        return WorkflowTriggerResult(
            stage=stage.key,
            label=stage.label,
            agent_name=stage.agent_name,
            accepted_at=accepted_at,
            poll_path=(f"/api/v1/agent-runs?agent_name={stage.agent_name}&limit=1"),
        )

    async def _execute(self, stage: WorkflowStage) -> None:
        try:
            await self._coordinator.run(
                stage.agent_name,
                stage.operation,
                reject_if_busy=True,
                label=stage.label,
            )
        except RunInProgressError:
            logger.warning(
                "Workflow trigger skipped because the agent is already running",
                extra={"stage": stage.key, "agent": stage.agent_name},
            )
        except asyncio.CancelledError:
            logger.info(
                "Workflow task cancelled during application shutdown",
                extra={"stage": stage.key, "agent": stage.agent_name},
            )
            raise
        except Exception:
            # Agent implementations persist their own failure state. This boundary
            # only prevents an unobserved task exception from escaping the event loop.
            logger.exception(
                "Workflow task failed",
                extra={"stage": stage.key, "agent": stage.agent_name},
            )

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending_agents.clear()
