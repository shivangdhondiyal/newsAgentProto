from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    NarrationAudio,
    NewsScript,
    ProductionJob,
    ProductionStage,
    ResearchBrief,
    ScenePlan,
)
from backend.app.schemas.production import ProductionJobCreate
from backend.app.services.production import (
    ProductionError,
    ProductionService,
    build_config,
    list_stories,
    load_defaults,
    reconcile_stale_production_jobs,
    save_defaults,
)
from backend.app.services.run_control import AgentRunCoordinator


async def _seed(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/story",
            canonical_url="https://bbc.example/story",
            title="Earthquake strikes city",
            normalized_text="Officials said an earthquake struck the city.",
        )
        session.add(article)
        await session.flush()
        brief = ResearchBrief(
            article_id=article.id,
            status="completed",
            headline=article.title,
            key_facts=[],
            context=[],
            uncertainties=[],
            source_count=2,
            summarizer_name="test",
        )
        session.add(brief)
        await session.flush()
        script = NewsScript(
            article_id=article.id,
            research_brief_id=brief.id,
            status="draft",
            title=article.title,
            hook="Here is what is known.",
            introduction="Independent sources corroborate the report.",
            main_story=["Officials said an earthquake struck the city."],
            context=[],
            closing="More information may follow.",
            full_text="A neutral narration.",
            estimated_duration_seconds=10,
            target_duration_seconds=60,
            writer_name="test",
            review_reasons=[],
        )
        session.add(script)
        await session.commit()
        script_id = script.id
    return engine, sessions, script_id


def _service(settings: Settings, sessions) -> ProductionService:
    return ProductionService(
        settings,
        sessions,
        AgentRunCoordinator(),
        script_writer_agent=None,
        scene_planner_agent=None,
        asset_agent=None,
        tts_agent=None,
        video_agent=None,
    )


def test_config_exposes_all_styles_and_licensing(tmp_path: Path) -> None:
    settings = Settings(production_defaults_file=tmp_path / "defaults.json")
    config = build_config(settings)
    keys = {style["key"] for style in config["styles"]}
    assert len(config["styles"]) == 18
    assert {"broadcast", "vox", "data", "social"} <= keys
    assert "wikimedia" in config["asset_providers"]
    assert config["aspect_options"] == ["both", "16:9", "9:16"]
    assert "never bundled" in config["licensing_note"]
    assert config["defaults"]["style_key"] == settings.video_default_style


def test_defaults_round_trip(tmp_path: Path) -> None:
    settings = Settings(production_defaults_file=tmp_path / "defaults.json")
    updated = save_defaults(settings, {"style_key": "vox", "aspect_ratio": "9:16"})
    assert updated["style_key"] == "vox"
    assert updated["aspect_ratio"] == "9:16"
    assert load_defaults(settings)["style_key"] == "vox"


@pytest.mark.asyncio
async def test_create_job_rejects_unknown_style_and_deduplicates(
    tmp_path: Path,
) -> None:
    engine, sessions, script_id = await _seed(tmp_path)
    settings = Settings(production_defaults_file=tmp_path / "defaults.json")
    service = _service(settings, sessions)

    with pytest.raises(ProductionError, match="Unknown editing style"):
        await service.create_job(
            ProductionJobCreate(script_id=script_id, style_key="not-a-style")
        )

    first = await service.create_job(
        ProductionJobCreate(script_id=script_id, style_key="vox", aspect_ratio="both")
    )
    second = await service.create_job(
        ProductionJobCreate(script_id=script_id, style_key="vox", aspect_ratio="both")
    )
    assert first.id == second.id

    async with sessions() as session:
        jobs = list((await session.scalars(select(ProductionJob))).all())
        stages = list(
            (
                await session.scalars(
                    select(ProductionStage).where(ProductionStage.job_id == first.id)
                )
            ).all()
        )
    assert len(jobs) == 1
    assert {stage.key for stage in stages} == {
        "prepare",
        "scenes",
        "assets",
        "narrate",
        "render",
        "verify",
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_stories_report_production_readiness(tmp_path: Path) -> None:
    engine, sessions, script_id = await _seed(tmp_path)
    settings = Settings()
    async with sessions() as session:
        stories = await list_stories(session, settings)
    assert len(stories) == 1
    assert stories[0]["script_id"] == script_id
    assert stories[0]["has_scene_plan"] is False
    assert stories[0]["asset_ready"] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_marks_running_jobs_interrupted(tmp_path: Path) -> None:
    engine, sessions, script_id = await _seed(tmp_path)
    settings = Settings(production_defaults_file=tmp_path / "defaults.json")
    service = _service(settings, sessions)
    job = await service.create_job(ProductionJobCreate(script_id=script_id))
    async with sessions() as session:
        stored = await session.get(ProductionJob, job.id)
        stored.status = "running"
        stage = await session.scalar(
            select(ProductionStage).where(ProductionStage.job_id == job.id)
        )
        stage.status = "running"
        await session.commit()

    reconciled = await reconcile_stale_production_jobs(sessions)
    assert reconciled == 1
    async with sessions() as session:
        stored = await session.get(ProductionJob, job.id)
        stage = await session.scalar(
            select(ProductionStage).where(ProductionStage.job_id == job.id)
        )
    assert stored.status == "interrupted"
    assert stage.status == "interrupted"
    await engine.dispose()

class _FakeTTS:
    def __init__(self, wav_path: Path) -> None:
        self.wav_path = wav_path
        self.calls: list[bool] = []

    async def execute(self, *, script_ids=None, force: bool = False):
        self.calls.append(force)
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        self.wav_path.write_bytes(b"RIFF")
        return None


async def _seed_narration(tmp_path: Path):
    engine, sessions, script_id = await _seed(tmp_path)
    wav_path = tmp_path / "mastered" / "narration-mastered.wav"
    async with sessions() as session:
        plan = ScenePlan(
            script_id=script_id,
            status="draft",
            total_duration_seconds=10,
            planner_name="test",
        )
        session.add(plan)
        await session.flush()
        session.add(
            NarrationAudio(
                script_id=script_id,
                scene_plan_id=plan.id,
                status="completed",
                provider_name="test",
                voice_name="test-voice",
                wav_path=str(wav_path),
                mp3_path=None,
                duration_seconds=2.5,
                sample_rate=16_000,
            )
        )
        await session.commit()
    return engine, sessions, script_id, wav_path


def _service_with_tts(settings: Settings, sessions, tts) -> ProductionService:
    return ProductionService(
        settings,
        sessions,
        AgentRunCoordinator(),
        script_writer_agent=None,
        scene_planner_agent=None,
        asset_agent=None,
        tts_agent=tts,
        video_agent=None,
    )


@pytest.mark.asyncio
async def test_narrate_stage_regenerates_when_the_mastered_file_is_missing(
    tmp_path: Path,
) -> None:
    engine, sessions, script_id, wav_path = await _seed_narration(tmp_path)
    assert not wav_path.is_file()
    settings = Settings(production_defaults_file=tmp_path / "defaults.json")
    fake = _FakeTTS(wav_path)
    service = _service_with_tts(settings, sessions, fake)
    job = await service.create_job(ProductionJobCreate(script_id=script_id))

    result = await service._stage_narrate(job.id)

    assert fake.calls == [True]
    assert wav_path.is_file()
    assert "Narrated" in result.message
    async with sessions() as session:
        stored = await session.get(ProductionJob, job.id)
    assert any("missing from disk" in warning for warning in stored.warnings)
    await engine.dispose()


@pytest.mark.asyncio
async def test_narrate_stage_reuses_an_existing_mastered_file(tmp_path: Path) -> None:
    engine, sessions, script_id, wav_path = await _seed_narration(tmp_path)
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(b"RIFF")
    settings = Settings(production_defaults_file=tmp_path / "defaults.json")
    fake = _FakeTTS(wav_path)
    service = _service_with_tts(settings, sessions, fake)
    job = await service.create_job(ProductionJobCreate(script_id=script_id))

    result = await service._stage_narrate(job.id)

    assert fake.calls == []
    assert "Using mastered narration" in result.message
    await engine.dispose()


@pytest.mark.asyncio
async def test_start_requeues_an_interrupted_stage(tmp_path: Path, monkeypatch) -> None:
    engine, sessions, script_id = await _seed(tmp_path)
    settings = Settings(production_defaults_file=tmp_path / "defaults.json")
    service = _service(settings, sessions)
    monkeypatch.setattr(service, "_spawn", lambda job_id: None)
    job = await service.create_job(ProductionJobCreate(script_id=script_id))
    async with sessions() as session:
        stored = await session.get(ProductionJob, job.id)
        stored.status = "interrupted"
        stored.error = "The process stopped while this production job was running."
        render = await session.scalar(
            select(ProductionStage).where(
                ProductionStage.job_id == job.id,
                ProductionStage.key == "render",
            )
        )
        render.status = "interrupted"
        render.error = "The process stopped while this stage was running."
        verify = await session.scalar(
            select(ProductionStage).where(
                ProductionStage.job_id == job.id,
                ProductionStage.key == "verify",
            )
        )
        verify.status = "failed"
        verify.error = "No video render is attached to this job"
        await session.commit()

    await service.start_job(job.id)

    async with sessions() as session:
        render = await session.scalar(
            select(ProductionStage).where(
                ProductionStage.job_id == job.id,
                ProductionStage.key == "render",
            )
        )
        verify = await session.scalar(
            select(ProductionStage).where(
                ProductionStage.job_id == job.id,
                ProductionStage.key == "verify",
            )
        )
        stored = await session.get(ProductionJob, job.id)
    assert render.status == "queued"
    assert render.error is None
    assert verify.status == "queued"
    assert verify.error is None
    assert stored.status == "running"
    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_requeues_interrupted_stages(tmp_path: Path, monkeypatch) -> None:
    engine, sessions, script_id = await _seed(tmp_path)
    settings = Settings(production_defaults_file=tmp_path / "defaults.json")
    service = _service(settings, sessions)
    monkeypatch.setattr(service, "_spawn", lambda job_id: None)
    job = await service.create_job(ProductionJobCreate(script_id=script_id))
    async with sessions() as session:
        stored = await session.get(ProductionJob, job.id)
        stored.status = "interrupted"
        render = await session.scalar(
            select(ProductionStage).where(
                ProductionStage.job_id == job.id,
                ProductionStage.key == "render",
            )
        )
        render.status = "interrupted"
        await session.commit()

    await service.retry_job(job.id)

    async with sessions() as session:
        render = await session.scalar(
            select(ProductionStage).where(
                ProductionStage.job_id == job.id,
                ProductionStage.key == "render",
            )
        )
        stored = await session.get(ProductionJob, job.id)
    assert render.status == "queued"
    assert stored.status == "running"
    await engine.dispose()
