from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.errors import AgentRunFailed
from backend.app.agents.scene_planner.contracts import PlannedScene, ScenePlanner
from backend.app.agents.scene_planner.planner import RuleBasedScenePlanner
from backend.app.core.config import Settings
from backend.app.db.models import AgentRun, NewsScript, Scene, ScenePlan
from backend.app.schemas.scene import ScenePlanRunRead

logger = logging.getLogger(__name__)


class ScenePlannerAgent:
    """Creates an editable visual timeline for scripts that passed the script guardrails."""

    name = "scene_planner"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        planner: ScenePlanner | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._planner = planner or RuleBasedScenePlanner()

    async def execute(self) -> ScenePlanRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                scripts = await self._load_eligible_scripts(session)
                run.items_seen = len(scripts)
                for script in scripts:
                    planned_scenes = self._planner.plan(script)
                    if not planned_scenes:
                        raise ValueError(f"Script {script.id} produced no visual scenes")
                    durations = self._durations(planned_scenes, script.estimated_duration_seconds)
                    plan = ScenePlan(
                        script_id=script.id,
                        status="draft",
                        total_duration_seconds=sum(durations),
                        planner_name=self._planner.name,
                    )
                    session.add(plan)
                    await session.flush()
                    start_seconds = 0
                    for position, (scene, duration) in enumerate(
                        zip(planned_scenes, durations, strict=True), start=1
                    ):
                        session.add(
                            Scene(
                                scene_plan_id=plan.id,
                                position=position,
                                start_seconds=start_seconds,
                                duration_seconds=duration,
                                narration=scene.narration,
                                subtitle=scene.subtitle,
                                keywords=scene.keywords,
                                scene_description=scene.scene_description,
                                stock_footage_query=scene.stock_footage_query,
                                image_search_query=scene.image_search_query,
                                animation_suggestion=scene.animation_suggestion,
                                transition=scene.transition,
                            )
                        )
                        start_seconds += duration
                    run.items_created += 1
                run.status = "completed"
            except Exception as exc:
                logger.exception("Scene planning run failed")
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise AgentRunFailed(self.name, run.id, run.errors[-1], run.errors)
        return ScenePlanRunRead.model_validate(run)

    async def _load_eligible_scripts(self, session: AsyncSession) -> list[NewsScript]:
        statement = (
            select(NewsScript)
            .outerjoin(ScenePlan, ScenePlan.script_id == NewsScript.id)
            .where(NewsScript.status == "draft", ScenePlan.id.is_(None))
            .order_by(NewsScript.created_at.asc())
            .limit(self._settings.scene_planner_batch_size)
        )
        return list((await session.scalars(statement)).all())

    def _durations(self, scenes: list[PlannedScene], target_seconds: int) -> list[int]:
        word_counts = [max(1, len(re.findall(r"\b\w+\b", scene.narration))) for scene in scenes]
        minimum = self._settings.scene_min_duration_seconds
        available = max(target_seconds, minimum * len(scenes))
        total_words = sum(word_counts)
        durations = [max(minimum, round(available * count / total_words)) for count in word_counts]
        difference = available - sum(durations)
        durations[-1] = max(minimum, durations[-1] + difference)
        return durations
