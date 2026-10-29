from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.agents.assets import AssetAgent
from backend.app.agents.credibility import CredibilityAgent
from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.agents.deduplication import DeduplicationAgent
from backend.app.agents.news_collector import NewsCollectorAgent
from backend.app.agents.research import ResearchAgent
from backend.app.agents.scene_planner import ScenePlannerAgent
from backend.app.agents.script_writer import ScriptWriterAgent
from backend.app.agents.tts import TTSAgent
from backend.app.agents.video import VideoGeneratorAgent
from backend.app.api import (
    captions,
    dashboard,
    production,
    publishing,
    reviews,
    system,
    workflow,
)
from backend.app.api.routes import router
from backend.app.core.config import get_settings
from backend.app.core.logging import configure_logging
from backend.app.db.session import (
    create_engine,
    create_schema,
    create_session_factory,
    reconcile_stale_agent_runs,
)
from backend.app.publishers.agent import PublishingAgent
from backend.app.services.captions import CaptionService
from backend.app.services.production import (
    ProductionService,
    reconcile_stale_production_jobs,
)
from backend.app.services.run_control import AgentRunCoordinator
from backend.app.services.workflow_dispatch import WorkflowDispatcher, WorkflowStage
from backend.app.services.workflow_stages import STAGE_SPECS

logger = logging.getLogger(__name__)


def _scheduled_job(
    coordinator: AgentRunCoordinator,
    *,
    agent_name: str,
    label: str,
    operation: Any,
):
    """Wrap a scheduled agent so it cannot race a manual trigger.

    Scheduled work skips a tick when the agent is already busy. Manual triggers
    receive an explicit conflict instead of queueing a duplicate execution.
    """

    async def job() -> None:
        await coordinator.run(
            agent_name,
            operation,
            reject_if_busy=False,
            label=label,
        )

    job.__name__ = f"scheduled_{agent_name}"
    return job


def _build_stages(app: FastAPI) -> list[WorkflowStage]:
    operations = {
        "news_collector": app.state.collector_agent.execute,
        "deduplication": app.state.deduplication_agent.execute,
        "credibility": app.state.credibility_agent.execute,
        "research": app.state.research_agent.execute,
        "script_writer": app.state.script_writer_agent.execute,
        "scene_planner": app.state.scene_planner_agent.execute,
        "asset_retriever": app.state.asset_agent.execute,
        "tts": app.state.tts_agent.execute,
        "video_generator": app.state.video_generator_agent.execute,
        "publishing": app.state.publishing_agent.execute,
    }
    return [
        WorkflowStage(
            key=spec.key,
            label=spec.label,
            description=spec.description,
            agent_name=spec.agent_name,
            operation=operations[spec.agent_name],
        )
        for spec in STAGE_SPECS
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    await create_schema(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.settings = settings
    app.state.trust_policy = SourceTrustPolicy.from_file(settings.trust_policy_file)
    app.state.collector_agent = NewsCollectorAgent(settings, session_factory)
    app.state.deduplication_agent = DeduplicationAgent(settings, session_factory)
    app.state.credibility_agent = CredibilityAgent(settings, session_factory)
    app.state.research_agent = ResearchAgent(settings, session_factory)
    app.state.script_writer_agent = ScriptWriterAgent(settings, session_factory)
    app.state.scene_planner_agent = ScenePlannerAgent(settings, session_factory)
    app.state.asset_agent = AssetAgent(settings, session_factory)
    app.state.tts_agent = TTSAgent(settings, session_factory)
    app.state.caption_service = CaptionService(settings, session_factory)
    app.state.video_generator_agent = VideoGeneratorAgent(
        settings,
        session_factory,
        caption_service=app.state.caption_service,
    )
    app.state.publishing_agent = PublishingAgent(settings, session_factory)

    # A process that stopped mid-run cannot execute its finally block. Close those
    # audit rows before the scheduler starts so the dashboard never shows a stale
    # "running" stage forever.
    interrupted = await reconcile_stale_agent_runs(session_factory)
    if interrupted:
        logger.warning("Reconciled %s interrupted agent run(s)", interrupted)
    interrupted_jobs = await reconcile_stale_production_jobs(session_factory)
    if interrupted_jobs:
        logger.warning("Reconciled %s interrupted production job(s)", interrupted_jobs)

    coordinator = AgentRunCoordinator()
    app.state.run_coordinator = coordinator
    app.state.production_service = ProductionService(
        settings,
        session_factory,
        coordinator,
        script_writer_agent=app.state.script_writer_agent,
        scene_planner_agent=app.state.scene_planner_agent,
        asset_agent=app.state.asset_agent,
        tts_agent=app.state.tts_agent,
        video_agent=app.state.video_generator_agent,
        caption_service=app.state.caption_service,
    )
    app.state.workflow_dispatcher = WorkflowDispatcher(coordinator, _build_stages(app))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduled = [
        (
            settings.collection_enabled,
            "news_collector",
            "Collect news",
            settings.collection_interval_minutes,
            app.state.collector_agent.execute,
        ),
        (
            settings.deduplication_enabled,
            "deduplication",
            "Deduplicate",
            settings.deduplication_interval_minutes,
            app.state.deduplication_agent.execute,
        ),
        (
            settings.credibility_enabled,
            "credibility",
            "Check credibility",
            settings.credibility_interval_minutes,
            app.state.credibility_agent.execute,
        ),
        (
            settings.research_enabled,
            "research",
            "Build research",
            settings.research_interval_minutes,
            app.state.research_agent.execute,
        ),
        (
            settings.script_writer_enabled,
            "script_writer",
            "Write scripts",
            settings.script_writer_interval_minutes,
            app.state.script_writer_agent.execute,
        ),
        (
            settings.scene_planner_enabled,
            "scene_planner",
            "Plan scenes",
            settings.scene_planner_interval_minutes,
            app.state.scene_planner_agent.execute,
        ),
        (
            settings.tts_enabled,
            "tts",
            "Narrate",
            settings.tts_interval_minutes,
            app.state.tts_agent.execute,
        ),
        (
            settings.video_enabled,
            "video_generator",
            "Render video",
            settings.video_interval_minutes,
            app.state.video_generator_agent.execute,
        ),
        (
            settings.publishing_enabled,
            "publishing",
            "Process publishing",
            settings.publishing_interval_minutes,
            app.state.publishing_agent.execute,
        ),
    ]
    for enabled, agent_name, label, minutes, operation in scheduled:
        if not enabled:
            continue
        scheduler.add_job(
            _scheduled_job(
                coordinator,
                agent_name=agent_name,
                label=label,
                operation=operation,
            ),
            trigger="interval",
            minutes=minutes,
            id=agent_name.replace("_", "-"),
            replace_existing=True,
        )
    if scheduler.get_jobs():
        scheduler.start()
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await app.state.production_service.shutdown()
        await app.state.workflow_dispatcher.shutdown()
        await engine.dispose()


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception:
        # Starlette's error handler converts this into a response; the identifier
        # is still attached by the exception handler below.
        raise
    response.headers["X-Request-ID"] = request_id
    return response


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        payload = {"request_id": _request_id(request), **detail}
    else:
        payload = {
            "code": "http_error",
            "message": str(detail),
            "request_id": _request_id(request),
        }
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": payload},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = [
        {
            "location": ".".join(str(part) for part in error.get("loc", ())),
            "message": error.get("msg", "Invalid value"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": "The request was not valid.",
                "errors": errors,
                "request_id": _request_id(request),
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = _request_id(request)
    logger.exception(
        "Unhandled request failure",
        extra={"request_id": request_id, "path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "internal_error",
                "message": "An unexpected server error occurred.",
                "request_id": request_id,
            }
        },
    )


app.include_router(router)
app.include_router(workflow.router, prefix="/api/v1")
app.include_router(reviews.router, prefix="/api/v1")
app.include_router(production.router, prefix="/api/v1")
app.include_router(captions.router, prefix="/api/v1")
app.include_router(publishing.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
app.include_router(dashboard.router)
