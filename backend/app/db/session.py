from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import AgentRun

STALE_RUN_MESSAGE = (
    "The application stopped before this run completed. The run was marked failed "
    "during startup reconciliation; retry the stage when the service is available."
)

STALE_RUN_SWEPT_MESSAGE = (
    "This run stopped updating its audit row (for example after a database lock) "
    "and is no longer executing in this process. It was marked failed by the "
    "stale-run sweeper; retry the stage."
)

# One token per application process. Audit rows are stamped at insert time so the
# sweeper can tell its own abandoned rows from a run another process still owns.
PROCESS_TOKEN = uuid.uuid4().hex


@event.listens_for(AgentRun, "before_insert")
def _stamp_run_owner(_mapper, _connection, target: AgentRun) -> None:
    if not target.owner_token:
        target.owner_token = PROCESS_TOKEN


def create_engine(settings: Settings) -> AsyncEngine:
    database_url = make_url(settings.database_url)
    is_sqlite = database_url.get_backend_name() == "sqlite"
    is_file_sqlite = is_sqlite and database_url.database not in (None, ":memory:")
    if is_file_sqlite:
        Path(database_url.database).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False, "timeout": 30.0} if is_sqlite else {}
    engine = create_async_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                # Wait for a competing writer instead of failing immediately with
                # "database is locked" while a scheduled agent holds a transaction.
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA foreign_keys=ON")
                if is_file_sqlite:
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# Lightweight additive migrations for SQLite deployments that already have data.
# ``create_all`` creates missing tables but never adds columns to an existing
# table, so new production fields are applied explicitly and idempotently.
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "agent_runs": {
        "owner_token": "VARCHAR(64)",
    },
    "production_jobs": {
        "genre_key": "VARCHAR(60) DEFAULT 'general_news'",
        "genre_profile_json": "JSON DEFAULT '{}'",
    },
    "scenes": {
        "editing_plan_json": "JSON DEFAULT '{}'",
        "graphic_plan_json": "JSON DEFAULT '{}'",
    },
    "narration_audio": {
        "delivery_json": "JSON DEFAULT '{}'",
    },
    "audio_masters": {
        "mix_path": "TEXT",
        "mix_json": "JSON DEFAULT '{}'",
    },
}


def _apply_additive_columns(connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table, columns in ADDITIVE_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, definition in columns.items():
            if name in existing:
                continue
            connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'))


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if engine.url.get_backend_name() == "sqlite":
            await connection.run_sync(_apply_additive_columns)


async def reconcile_stale_agent_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reason: str = STALE_RUN_MESSAGE,
    min_age_seconds: float = 0.0,
    exclude_agents: set[str] | None = None,
    own_only: bool = False,
) -> int:
    """Fail audit rows left in ``running`` by a previous process.

    A stopped server cannot execute the ``finally`` block that closes an agent run,
    so the row would otherwise appear active forever in the dashboard. Reconciliation
    runs once during startup, before the scheduler or any request can start new work.

    ``min_age_seconds`` and ``exclude_agents`` exist for the periodic sweeper: a run
    whose final audit write failed (a SQLite lock, for example) keeps its ``running``
    row while the agent itself is idle. The sweeper must never touch a run that the
    coordinator still holds, hence the exclusion list.
    """

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=min_age_seconds)
    skipped = exclude_agents or set()
    async with session_factory() as session:
        runs = list(
            (await session.scalars(select(AgentRun).where(AgentRun.status == "running"))).all()
        )
        stale: list[AgentRun] = []
        for run in runs:
            if run.agent_name in skipped:
                continue
            if own_only and run.owner_token != PROCESS_TOKEN:
                # Another process owns this row (or it predates owner tokens).
                # Only startup reconciliation may close those.
                continue
            started_at = run.started_at
            if started_at is not None and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            if started_at is not None and started_at > cutoff:
                continue
            stale.append(run)
        for run in stale:
            started_at = run.started_at
            if started_at is not None and started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            run.status = "failed"
            run.finished_at = now
            run.duration_ms = (
                round((now - started_at).total_seconds() * 1000, 2)
                if started_at is not None
                else run.duration_ms
            )
            run.errors = [*(run.errors or []), reason]
            run.details = {
                **(run.details or {}),
                "interrupted": True,
                "reconciled_at": now.isoformat(),
            }
        if stale:
            await session.commit()
    return len(stale)


async def get_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
