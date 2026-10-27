from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class RunInProgressError(RuntimeError):
    """Raised when a caller asks to start an agent that is already executing."""

    def __init__(self, agent_name: str, label: str | None = None) -> None:
        self.agent_name = agent_name
        self.label = label or agent_name.replace("_", " ").title()
        super().__init__(f"{self.label} is already running")


class AgentRunCoordinator:
    """Serializes each agent's work within the current application process.

    The scheduler and HTTP workflow triggers share one coordinator. This prevents a
    manual trigger from racing a scheduled run (and vice versa), which is especially
    important for SQLite-backed deployments.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, agent_name: str) -> asyncio.Lock:
        lock = self._locks.get(agent_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_name] = lock
        return lock

    def is_running(self, agent_name: str) -> bool:
        return self._lock_for(agent_name).locked()

    async def run(
        self,
        agent_name: str,
        operation: Callable[[], Awaitable[T]],
        *,
        reject_if_busy: bool = False,
        wait_if_busy: bool = False,
        label: str | None = None,
    ) -> T | None:
        """Run an operation under the agent's process-local lock.

        Scheduled jobs pass ``reject_if_busy=False`` so a missed interval is skipped
        rather than queued behind a long manual run. HTTP triggers pass ``True`` so
        the caller receives an explicit conflict instead of a duplicate execution.
        Production jobs pass ``wait_if_busy=True`` because their stages are sequential
        and must complete rather than silently skip.
        """

        lock = self._lock_for(agent_name)
        if lock.locked() and not wait_if_busy:
            if reject_if_busy:
                raise RunInProgressError(agent_name, label)
            return None
        async with lock:
            return await operation()
