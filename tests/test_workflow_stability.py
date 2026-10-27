from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.api import system, workflow
from backend.app.core.config import Settings
from backend.app.db.models import AgentRun
from backend.app.db.session import (
    create_engine,
    create_schema,
    create_session_factory,
    reconcile_stale_agent_runs,
)
from backend.app.services.run_control import AgentRunCoordinator, RunInProgressError
from backend.app.services.workflow_dispatch import (
    WorkflowDispatcher,
    WorkflowStage,
    WorkflowStageNotFound,
)
from tests.support import seed_content


@pytest.mark.asyncio
async def test_dispatcher_rejects_duplicate_trigger_for_same_agent() -> None:
    coordinator = AgentRunCoordinator()
    release = asyncio.Event()
    calls: list[str] = []

    async def operation() -> str:
        calls.append("started")
        await release.wait()
        return "completed"

    dispatcher = WorkflowDispatcher(
        coordinator,
        [
            WorkflowStage(
                key="demo",
                label="Demo stage",
                description="Test stage",
                agent_name="demo_agent",
                operation=operation,
            )
        ],
    )

    result = dispatcher.trigger("demo")
    assert result.stage == "demo"
    assert result.agent_name == "demo_agent"
    assert result.poll_path == "/api/v1/agent-runs?agent_name=demo_agent&limit=1"

    with pytest.raises(RunInProgressError):
        dispatcher.trigger("demo")
    with pytest.raises(WorkflowStageNotFound):
        dispatcher.trigger("missing")

    release.set()
    await asyncio.sleep(0.05)
    assert calls == ["started"]
    await dispatcher.shutdown()


@pytest.mark.asyncio
async def test_workflow_trigger_returns_accepted_conflict_and_unknown_stage() -> None:
    coordinator = AgentRunCoordinator()
    release = asyncio.Event()

    async def operation() -> None:
        await release.wait()

    dispatcher = WorkflowDispatcher(
        coordinator,
        [
            WorkflowStage(
                key="demo",
                label="Demo stage",
                description="Test stage",
                agent_name="demo_agent",
                operation=operation,
            )
        ],
    )
    app = FastAPI()
    app.state.workflow_dispatcher = dispatcher
    app.include_router(workflow.router, prefix="/api/v1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.post("/api/v1/workflow/runs", json={"stage": "demo"})
        assert accepted.status_code == 202
        payload = accepted.json()
        assert payload["accepted"] is True
        assert payload["stage"] == "demo"
        assert payload["label"] == "Demo stage"

        duplicate = await client.post("/api/v1/workflow/runs", json={"stage": "demo"})
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "run_in_progress"
        assert duplicate.json()["detail"]["agent"] == "demo_agent"

        missing = await client.post("/api/v1/workflow/runs", json={"stage": "unknown"})
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "workflow_stage_not_found"

    release.set()
    await dispatcher.shutdown()


@pytest.mark.asyncio
async def test_startup_reconciliation_fails_interrupted_runs() -> None:
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    engine = create_engine(settings)
    await create_schema(engine)
    sessions = create_session_factory(engine)

    async with sessions() as session:
        session.add(
            AgentRun(
                agent_name="news_collector",
                status="running",
                started_at=datetime.now(UTC) - timedelta(seconds=10),
                details={"phase": "fetching"},
            )
        )
        session.add(
            AgentRun(
                agent_name="research",
                status="completed",
                started_at=datetime.now(UTC) - timedelta(minutes=2),
                finished_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.commit()

    reconciled = await reconcile_stale_agent_runs(sessions)
    assert reconciled == 1

    async with sessions() as session:
        runs = list((await session.scalars(select(AgentRun))).all())
        assert len(runs) == 2
        interrupted = next(row for row in runs if row.agent_name == "news_collector")
        assert interrupted.status == "failed"
        assert interrupted.finished_at is not None
        assert interrupted.duration_ms is not None
        assert interrupted.errors
        assert interrupted.details["interrupted"] is True
        completed = next(row for row in runs if row.agent_name == "research")
        assert completed.status == "completed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_overview_uses_real_counts_and_pipeline_stages(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    app = FastAPI()
    app.state.session_factory = seeded.sessions
    app.state.settings = seeded.settings
    app.state.scheduler = None
    app.state.run_coordinator = AgentRunCoordinator()
    app.include_router(system.router, prefix="/api/v1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/system/overview")
        assert response.status_code == 200
        payload = response.json()

    assert len(payload["pipeline"]) == 10
    stages = {stage["key"]: stage for stage in payload["pipeline"]}
    assert stages["collection"]["count"] == 1
    assert stages["deduplication"]["count"] == 1
    assert stages["credibility"]["count"] == 0
    assert stages["scenes"]["count"] == 0
    assert stages["assets"]["count"] == 1
    assert stages["video"]["count"] == 0
    assert stages["publishing"]["action_path"] == "/api/v1/workflow/runs"

    metrics = {metric["key"]: metric["value"] for metric in payload["metrics"]}
    assert metrics["collected"] == 1
    assert metrics["approvals"] == 0
    assert metrics["publishing_attention"] == 0
    assert payload["active_runs"] == 0
    assert payload["recent_runs"] == []
    assert payload["status"] in {"ok", "degraded"}

    await seeded.engine.dispose()
