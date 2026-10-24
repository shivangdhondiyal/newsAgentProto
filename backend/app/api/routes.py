from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import (
    Article,
    CredibilityAssessment,
    CredibilityEvidence,
    DuplicateCluster,
    DuplicateClusterMember,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    ResearchBrief,
    ResearchSource,
    Scene,
    ScenePlan,
    VideoRender,
)
from backend.app.schemas.article import ArticleRead, CollectionRunRead
from backend.app.schemas.credibility import (
    CredibilityAssessmentRead,
    CredibilityEvidenceRead,
    CredibilityRunRead,
)
from backend.app.schemas.deduplication import (
    DeduplicationRunRead,
    DuplicateClusterRead,
    DuplicateClusterResolution,
)
from backend.app.schemas.research import (
    ResearchBriefRead,
    ResearchRunRead,
    ResearchSourceRead,
)
from backend.app.schemas.scene import ScenePlanRead, ScenePlanRunRead, SceneRead
from backend.app.schemas.script import NewsScriptRead, ScriptRunRead
from backend.app.schemas.tts import NarrationAudioRead, NarrationChunkRead, TTSRunRead
from backend.app.schemas.video import VideoRenderRead, VideoRunRead

router = APIRouter(prefix="/api/v1")


async def get_db_session(request: Request):
    async with request.app.state.session_factory() as session:
        yield session


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/collection/runs", response_model=CollectionRunRead, tags=["collection"])
async def trigger_collection(request: Request) -> CollectionRunRead:
    try:
        return await request.app.state.collector_agent.execute()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="Collection run failed") from exc


@router.post("/deduplication/runs", response_model=DeduplicationRunRead, tags=["deduplication"])
async def trigger_deduplication(request: Request) -> DeduplicationRunRead:
    try:
        return await request.app.state.deduplication_agent.execute()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="Deduplication run failed") from exc


@router.post("/credibility/runs", response_model=CredibilityRunRead, tags=["credibility"])
async def trigger_credibility(request: Request) -> CredibilityRunRead:
    try:
        return await request.app.state.credibility_agent.execute()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="Credibility run failed") from exc


@router.post("/research/runs", response_model=ResearchRunRead, tags=["research"])
async def trigger_research(request: Request) -> ResearchRunRead:
    try:
        return await request.app.state.research_agent.execute()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="Research run failed") from exc


@router.post("/script-writer/runs", response_model=ScriptRunRead, tags=["scripts"])
async def trigger_script_writer(request: Request) -> ScriptRunRead:
    try:
        return await request.app.state.script_writer_agent.execute()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="Script writer run failed") from exc


@router.post("/scene-planner/runs", response_model=ScenePlanRunRead, tags=["scenes"])
async def trigger_scene_planner(request: Request) -> ScenePlanRunRead:
    try:
        return await request.app.state.scene_planner_agent.execute()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="Scene planning run failed") from exc


@router.post("/tts/runs", response_model=TTSRunRead, tags=["tts"])
async def trigger_tts(request: Request) -> TTSRunRead:
    try:
        return await request.app.state.tts_agent.execute()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="TTS run failed") from exc


@router.post("/video-generator/runs", response_model=VideoRunRead, tags=["video"])
async def trigger_video_generation(request: Request) -> VideoRunRead:
    try:
        return await request.app.state.video_generator_agent.execute()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="Video generation run failed") from exc


@router.get("/articles", response_model=list[ArticleRead], tags=["articles"])
async def list_articles(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    category: str | None = None,
    country: str | None = None,
    language: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[Article]:
    statement = select(Article).order_by(Article.collected_at.desc()).limit(limit)
    if category:
        statement = statement.where(Article.category == category)
    if country:
        statement = statement.where(Article.country == country)
    if language:
        statement = statement.where(Article.language == language)
    return list((await session.scalars(statement)).all())


@router.get(
    "/articles/{article_id}/credibility",
    response_model=CredibilityAssessmentRead,
    tags=["credibility"],
)
async def get_article_credibility(
    article_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CredibilityAssessmentRead:
    assessment = await session.scalar(
        select(CredibilityAssessment).where(CredibilityAssessment.article_id == article_id)
    )
    if assessment is None:
        raise HTTPException(
            status_code=404, detail="No credibility assessment exists for this article"
        )
    evidence = list(
        (
            await session.scalars(
                select(CredibilityEvidence)
                .where(CredibilityEvidence.assessment_id == assessment.id)
                .order_by(CredibilityEvidence.relevance_score.desc())
            )
        ).all()
    )
    return CredibilityAssessmentRead(
        id=assessment.id,
        article_id=assessment.article_id,
        status=assessment.status,
        confidence_score=assessment.confidence_score,
        explanation=assessment.explanation,
        claims=assessment.claims,
        independent_source_count=assessment.independent_source_count,
        extractor_name=assessment.extractor_name,
        created_at=assessment.created_at,
        updated_at=assessment.updated_at,
        evidence=[CredibilityEvidenceRead.model_validate(item) for item in evidence],
    )


@router.get(
    "/articles/{article_id}/research",
    response_model=ResearchBriefRead,
    tags=["research"],
)
async def get_article_research(
    article_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResearchBriefRead:
    brief = await session.scalar(
        select(ResearchBrief).where(ResearchBrief.article_id == article_id)
    )
    if brief is None:
        raise HTTPException(status_code=404, detail="No research brief exists for this article")
    sources = list(
        (
            await session.scalars(
                select(ResearchSource)
                .where(ResearchSource.brief_id == brief.id)
                .order_by(ResearchSource.relevance_score.desc())
            )
        ).all()
    )
    return ResearchBriefRead(
        id=brief.id,
        article_id=brief.article_id,
        status=brief.status,
        headline=brief.headline,
        key_facts=brief.key_facts,
        context=brief.context,
        uncertainties=brief.uncertainties,
        source_count=brief.source_count,
        summarizer_name=brief.summarizer_name,
        created_at=brief.created_at,
        updated_at=brief.updated_at,
        sources=[ResearchSourceRead.model_validate(source) for source in sources],
    )


@router.get(
    "/articles/{article_id}/script",
    response_model=NewsScriptRead,
    tags=["scripts"],
)
async def get_article_script(
    article_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> NewsScriptRead:
    script = await session.scalar(select(NewsScript).where(NewsScript.article_id == article_id))
    if script is None:
        raise HTTPException(status_code=404, detail="No script exists for this article")
    return NewsScriptRead.model_validate(script)


@router.get(
    "/scripts/{script_id}/scene-plan",
    response_model=ScenePlanRead,
    tags=["scenes"],
)
async def get_scene_plan(
    script_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ScenePlanRead:
    plan = await session.scalar(select(ScenePlan).where(ScenePlan.script_id == script_id))
    if plan is None:
        raise HTTPException(status_code=404, detail="No scene plan exists for this script")
    scenes = list(
        (
            await session.scalars(
                select(Scene).where(Scene.scene_plan_id == plan.id).order_by(Scene.position.asc())
            )
        ).all()
    )
    return ScenePlanRead(
        id=plan.id,
        script_id=plan.script_id,
        status=plan.status,
        total_duration_seconds=plan.total_duration_seconds,
        planner_name=plan.planner_name,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        scenes=[SceneRead.model_validate(scene) for scene in scenes],
    )


@router.get(
    "/scripts/{script_id}/narration",
    response_model=NarrationAudioRead,
    tags=["tts"],
)
async def get_narration(
    script_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> NarrationAudioRead:
    narration = await session.scalar(
        select(NarrationAudio).where(NarrationAudio.script_id == script_id)
    )
    if narration is None:
        raise HTTPException(status_code=404, detail="No narration exists for this script")
    chunks = list(
        (
            await session.scalars(
                select(NarrationChunk)
                .where(NarrationChunk.narration_audio_id == narration.id)
                .order_by(NarrationChunk.position.asc())
            )
        ).all()
    )
    return NarrationAudioRead(
        id=narration.id,
        script_id=narration.script_id,
        scene_plan_id=narration.scene_plan_id,
        status=narration.status,
        provider_name=narration.provider_name,
        voice_name=narration.voice_name,
        wav_path=narration.wav_path,
        mp3_path=narration.mp3_path,
        duration_seconds=narration.duration_seconds,
        sample_rate=narration.sample_rate,
        created_at=narration.created_at,
        updated_at=narration.updated_at,
        chunks=[NarrationChunkRead.model_validate(chunk) for chunk in chunks],
    )


@router.get(
    "/narrations/{narration_id}/video",
    response_model=VideoRenderRead,
    tags=["video"],
)
async def get_video_render(
    narration_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> VideoRenderRead:
    render = await session.scalar(
        select(VideoRender).where(VideoRender.narration_audio_id == narration_id)
    )
    if render is None:
        raise HTTPException(status_code=404, detail="No video render exists for this narration")
    return VideoRenderRead.model_validate(render)


@router.get(
    "/duplicate-clusters",
    response_model=list[DuplicateClusterRead],
    tags=["deduplication"],
)
async def list_duplicate_clusters(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cluster_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[DuplicateClusterRead]:
    statement = select(DuplicateCluster).order_by(DuplicateCluster.created_at.desc()).limit(limit)
    if cluster_status:
        statement = statement.where(DuplicateCluster.status == cluster_status)
    clusters = list((await session.scalars(statement)).all())
    result: list[DuplicateClusterRead] = []
    for cluster in clusters:
        members = list(
            (
                await session.scalars(
                    select(DuplicateClusterMember)
                    .where(DuplicateClusterMember.cluster_id == cluster.id)
                    .order_by(DuplicateClusterMember.role.desc())
                )
            ).all()
        )
        result.append(
            DuplicateClusterRead(
                id=cluster.id,
                canonical_article_id=cluster.canonical_article_id,
                status=cluster.status,
                created_at=cluster.created_at,
                resolved_at=cluster.resolved_at,
                members=members,
            )
        )
    return result


@router.post(
    "/duplicate-clusters/{cluster_id}/resolution",
    response_model=DuplicateClusterRead,
    tags=["deduplication"],
)
async def resolve_duplicate_cluster(
    cluster_id: str,
    resolution: DuplicateClusterResolution,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DuplicateClusterRead:
    cluster = await session.get(DuplicateCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Duplicate cluster not found")
    cluster.status = "confirmed" if resolution.action == "confirm" else "rejected"
    cluster.resolved_at = datetime.now(UTC)
    await session.commit()
    members = list(
        (
            await session.scalars(
                select(DuplicateClusterMember)
                .where(DuplicateClusterMember.cluster_id == cluster.id)
                .order_by(DuplicateClusterMember.role.desc())
            )
        ).all()
    )
    return DuplicateClusterRead(
        id=cluster.id,
        canonical_article_id=cluster.canonical_article_id,
        status=cluster.status,
        created_at=cluster.created_at,
        resolved_at=cluster.resolved_at,
        members=members,
    )
