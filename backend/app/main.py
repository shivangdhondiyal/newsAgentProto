from __future__ import annotations

from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from backend.app.agents.credibility import CredibilityAgent
from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.agents.deduplication import DeduplicationAgent
from backend.app.agents.news_collector import NewsCollectorAgent
from backend.app.agents.research import ResearchAgent
from backend.app.agents.scene_planner import ScenePlannerAgent
from backend.app.agents.script_writer import ScriptWriterAgent
from backend.app.agents.tts import TTSAgent
from backend.app.agents.video import VideoGeneratorAgent
from backend.app.api import dashboard, publishing, reviews, system
from backend.app.api.routes import router
from backend.app.core.config import get_settings
from backend.app.core.logging import configure_logging
from backend.app.db.session import create_engine, create_schema, create_session_factory
from backend.app.publishers.agent import PublishingAgent


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
    app.state.tts_agent = TTSAgent(settings, session_factory)
    app.state.video_generator_agent = VideoGeneratorAgent(settings, session_factory)
    app.state.publishing_agent = PublishingAgent(settings, session_factory)

    scheduler = AsyncIOScheduler(timezone="UTC")
    if settings.collection_enabled:
        scheduler.add_job(
            app.state.collector_agent.execute,
            trigger="interval",
            minutes=settings.collection_interval_minutes,
            id="news-collector",
            replace_existing=True,
        )
        scheduler.start()
    if settings.deduplication_enabled:
        scheduler.add_job(
            app.state.deduplication_agent.execute,
            trigger="interval",
            minutes=settings.deduplication_interval_minutes,
            id="deduplication",
            replace_existing=True,
        )
        if not scheduler.running:
            scheduler.start()
    if settings.credibility_enabled:
        scheduler.add_job(
            app.state.credibility_agent.execute,
            trigger="interval",
            minutes=settings.credibility_interval_minutes,
            id="credibility",
            replace_existing=True,
        )
        if not scheduler.running:
            scheduler.start()
    if settings.research_enabled:
        scheduler.add_job(
            app.state.research_agent.execute,
            trigger="interval",
            minutes=settings.research_interval_minutes,
            id="research",
            replace_existing=True,
        )
        if not scheduler.running:
            scheduler.start()
    if settings.script_writer_enabled:
        scheduler.add_job(
            app.state.script_writer_agent.execute,
            trigger="interval",
            minutes=settings.script_writer_interval_minutes,
            id="script-writer",
            replace_existing=True,
        )
        if not scheduler.running:
            scheduler.start()
    if settings.scene_planner_enabled:
        scheduler.add_job(
            app.state.scene_planner_agent.execute,
            trigger="interval",
            minutes=settings.scene_planner_interval_minutes,
            id="scene-planner",
            replace_existing=True,
        )
        if not scheduler.running:
            scheduler.start()
    if settings.tts_enabled:
        scheduler.add_job(
            app.state.tts_agent.execute,
            trigger="interval",
            minutes=settings.tts_interval_minutes,
            id="tts",
            replace_existing=True,
        )
        if not scheduler.running:
            scheduler.start()
    if settings.video_enabled:
        scheduler.add_job(
            app.state.video_generator_agent.execute,
            trigger="interval",
            minutes=settings.video_interval_minutes,
            id="video-generator",
            replace_existing=True,
        )
        if not scheduler.running:
            scheduler.start()
    if settings.publishing_enabled:
        scheduler.add_job(
            app.state.publishing_agent.execute,
            trigger="interval",
            minutes=settings.publishing_interval_minutes,
            id="publishing",
            replace_existing=True,
        )
        if not scheduler.running:
            scheduler.start()
    app.state.scheduler = scheduler
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await engine.dispose()


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
app.include_router(router)
app.include_router(reviews.router, prefix="/api/v1")
app.include_router(publishing.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
app.include_router(dashboard.router)
