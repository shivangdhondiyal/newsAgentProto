from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.errors import AgentRunFailed
from backend.app.core.config import Settings
from backend.app.db.models import (
    AgentRun,
    NewsScript,
    Scene,
    SceneAsset,
    ScenePlan,
    VisualAsset,
)
from backend.app.media.assets import AssetBrief, AssetPipeline, AssetSelection
from backend.app.media.keywords import proper_nouns, search_keywords
from backend.app.schemas.video import VideoRunRead


class AssetAgent:
    """Retrieve licensed, validated scene imagery and persist explicit fallbacks.

    A scene is never left without a documented decision: either a validated
    asset with provenance, or a designed-graphic fallback with the reason.
    """

    name = "asset_retriever"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        pipeline: AssetPipeline | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._pipeline = pipeline or AssetPipeline(settings)

    async def execute(
        self,
        *,
        script_id: str | None = None,
        strategy: str | None = None,
        aspect_ratio: str = "16:9",
    ) -> VideoRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                candidates = await self._load_eligible_scenes(session, script_id)
                run.items_seen = len(candidates)
                for scene, existing_asset in candidates:
                    created = await self._retrieve_for_scene(
                        session,
                        scene,
                        run,
                        existing=existing_asset,
                        strategy=strategy or self._settings.asset_strategy_default,
                        aspect_ratio=aspect_ratio,
                    )
                    if created:
                        run.items_created += 1
                    else:
                        run.items_updated += 1
                run.status = "completed"
            except Exception as exc:
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1_000, 2)
                await session.commit()
                await session.refresh(run)
        if run.status == "failed":
            raise AgentRunFailed(self.name, run.id, run.errors[-1], run.errors)
        return VideoRunRead.model_validate(run)

    async def _load_eligible_scenes(
        self, session: AsyncSession, script_id: str | None = None
    ) -> list[tuple[Scene, SceneAsset | None]]:
        statement = (
            select(Scene, SceneAsset, VisualAsset)
            .join(ScenePlan, ScenePlan.id == Scene.scene_plan_id)
            .join(NewsScript, NewsScript.id == ScenePlan.script_id)
            .outerjoin(SceneAsset, SceneAsset.scene_id == Scene.id)
            .outerjoin(VisualAsset, VisualAsset.id == SceneAsset.asset_id)
            .where(
                ScenePlan.status == "draft",
                NewsScript.status == "draft",
            )
            .order_by(ScenePlan.created_at.asc(), Scene.position.asc())
        )
        if script_id:
            statement = statement.where(NewsScript.id == script_id)
        eligible: list[tuple[Scene, SceneAsset | None]] = []
        for scene, scene_asset, asset in (await session.execute(statement)).all():
            if scene_asset is None:
                eligible.append((scene, None))
                continue
            # A row can outlive its cached file (disk cleanup, restore, sync
            # conflict). Re-retrieve instead of silently rendering a fallback.
            if scene_asset.status == "ready" and (
                asset is None or not Path(asset.local_path).is_file()
            ):
                eligible.append((scene, scene_asset))
        return eligible[: max(5, self._settings.scene_planner_batch_size * 4)]

    async def _retrieve_for_scene(
        self,
        session: AsyncSession,
        scene: Scene,
        run: AgentRun,
        *,
        existing: SceneAsset | None,
        strategy: str,
        aspect_ratio: str,
    ) -> bool:
        narration = scene.narration or scene.subtitle or ""
        query = self._scene_query(scene)
        brief = AssetBrief(
            scene_id=scene.id,
            query=query,
            keywords=search_keywords(narration),
            subjects=tuple(proper_nouns(narration)),
            description=scene.scene_description or "",
            prefer_aspect=aspect_ratio,
        )
        if existing is not None:
            run.warnings = [
                *run.warnings,
                (
                    f"Scene {scene.position}: the cached asset file was missing; "
                    "re-retrieved the visual."
                ),
            ]
        result = await self._pipeline.retrieve(
            brief,
            strategy=strategy,
            aspect_ratio=aspect_ratio,
        )
        run.warnings = [*run.warnings, *result.warnings]
        if result.selection is None:
            self._store_scene_asset(
                session,
                scene,
                existing,
                asset_id=None,
                relevance_score=0.0,
                match_reason=(
                    "Designed graphic fallback: "
                    + ("; ".join(result.warnings[-2:]) or "no licensed asset available")
                ),
                status="fallback",
            )
            return existing is None
        asset = await self._upsert_asset(session, result.selection, query)
        self._store_scene_asset(
            session,
            scene,
            existing,
            asset_id=asset.id,
            relevance_score=result.selection.rank_score,
            match_reason=(
                f"{result.selection.candidate.provider} match "
                f"({result.selection.rank_score:.2f} relevance, "
                f"{result.selection.width}x{result.selection.height})"
            ),
            status="ready",
        )
        return existing is None

    @staticmethod
    def _store_scene_asset(
        session: AsyncSession,
        scene: Scene,
        existing: SceneAsset | None,
        **fields,
    ) -> None:
        if existing is None:
            session.add(SceneAsset(scene_id=scene.id, role="primary", **fields))
            return
        for key, value in fields.items():
            setattr(existing, key, value)

    @staticmethod
    def _scene_query(scene: Scene) -> str:
        """Build a search query that a photo library can actually answer.

        Stored queries may come from older plans that included filler words,
        digits, or the phrase "editorial image". Re-deriving the subjects from
        the narration keeps retrieval relevant without re-planning the story.
        Wikimedia's search is effectively AND-based, so a long query returns
        nothing; named entities alone retrieve the best matches.
        """

        text = scene.narration or scene.subtitle or ""
        subjects = proper_nouns(text)
        keywords = search_keywords(text)
        terms: list[str] = []
        for term in [*subjects[:3], *keywords[:3]]:
            if term not in terms:
                terms.append(term)
        if len(terms) >= 2:
            return " ".join(terms[:4])
        stored = " ".join((scene.image_search_query or "").split())
        if stored and len(terms) < 2:
            return stored
        if terms:
            return " ".join(terms)
        return " ".join(scene.keywords or []) or scene.scene_description or "news context"

    async def _upsert_asset(
        self, session: AsyncSession, selection: AssetSelection, query: str
    ) -> VisualAsset:
        candidate = selection.candidate
        existing = await session.scalar(
            select(VisualAsset).where(
                VisualAsset.provider == candidate.provider,
                VisualAsset.provider_asset_id == candidate.provider_asset_id,
            )
        )
        if existing is not None:
            existing.local_path = str(selection.local_path)
            existing.checksum = selection.checksum
            existing.width = selection.width
            existing.height = selection.height
            existing.focal_x = selection.focal_x
            existing.focal_y = selection.focal_y
            existing.status = "valid"
            existing.query = query
            return existing
        asset = VisualAsset(
            provider=candidate.provider,
            provider_asset_id=candidate.provider_asset_id,
            query=query,
            title=candidate.title,
            description=candidate.description or None,
            source_url=candidate.source_url,
            download_url=candidate.download_url,
            creator=candidate.creator,
            license_name=candidate.license_name,
            license_url=candidate.license_url,
            attribution=candidate.attribution,
            media_type=candidate.media_type,
            local_path=str(selection.local_path),
            mime_type=selection.mime_type,
            file_size=selection.file_size,
            width=selection.width,
            height=selection.height,
            focal_x=selection.focal_x,
            focal_y=selection.focal_y,
            checksum=selection.checksum,
            status="valid",
            metadata_json={
                "relevance_score": selection.rank_score,
                "candidate_metadata": candidate.metadata,
            },
        )
        session.add(asset)
        await session.flush()
        return asset
