from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.api.reviews import edit_scene, edit_script, get_review, list_reviews
from backend.app.db.models import (
    ApprovalEvent,
    ContentApproval,
    NarrationAudio,
    Scene,
    ScenePlan,
    VideoRender,
)
from backend.app.reviews.quality import checks_passed, sha256_file
from backend.app.reviews.service import (
    ApprovalBlockedError,
    decide_approval,
    get_or_create_approval,
)
from backend.app.schemas.review import SceneUpdate, ScriptUpdate
from tests.support import seed_content


def _fake_request() -> SimpleNamespace:
    policy = SourceTrustPolicy.from_file(Path("config/trust.yml"))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(trust_policy=policy)))


@pytest.mark.asyncio
async def test_quality_gate_approval_pins_artifact_hash(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    async with seeded.sessions() as session:
        render = await session.get(VideoRender, seeded.video_id)
        from backend.app.db.models import NewsScript

        script = await session.get(NewsScript, seeded.script_id)
        approval = await get_or_create_approval(session, render, script, seeded.settings)
        assert approval.status == "pending"
        assert checks_passed(approval.quality_checks)
        await decide_approval(
            session,
            render=render,
            script=script,
            approval=approval,
            action="approve",
            reviewer="editor",
            notes="Sources and script checked.",
            settings=seeded.settings,
        )
        await session.commit()

        assert approval.status == "approved"
        assert render.status == "approved"
        assert approval.artifact_sha256 == sha256_file(seeded.video_path)
        event = await session.scalar(select(ApprovalEvent))
        assert event is not None
        assert event.action == "approved"
        assert event.actor == "editor"
    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_quality_gate_blocks_small_or_flagged_video(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path, video_bytes=100)
    async with seeded.sessions() as session:
        render = await session.get(VideoRender, seeded.video_id)
        from backend.app.db.models import NewsScript

        script = await session.get(NewsScript, seeded.script_id)
        approval = await get_or_create_approval(session, render, script, seeded.settings)
        assert not checks_passed(approval.quality_checks)
        with pytest.raises(ApprovalBlockedError) as error:
            await decide_approval(
                session,
                render=render,
                script=script,
                approval=approval,
                action="approve",
                reviewer="editor",
                notes=None,
                settings=seeded.settings,
            )
        assert any(
            check["name"] == "video_file" and check["status"] == "failed"
            for check in error.value.checks
        )
        assert approval.status == "pending"
    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_script_edit_requires_acknowledgement_and_clears_downstream(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    async with seeded.sessions() as session:
        with pytest.raises(HTTPException) as error:
            await edit_script(
                seeded.script_id,
                ScriptUpdate(title="Updated headline"),
                session,
            )
        assert error.value.status_code == 409

        result = await edit_script(
            seeded.script_id,
            ScriptUpdate(
                title="Updated headline",
                closing="A revised and carefully verified closing.",
                acknowledge_media_invalidation=True,
                actor="editor",
            ),
            session,
        )
        assert result.invalidated is True
        from backend.app.db.models import NewsScript

        script = await session.get(NewsScript, seeded.script_id)
        assert script.title == "Updated headline"
        assert script.closing == "A revised and carefully verified closing."
        assert "updated" not in script.full_text.lower()  # title is metadata, not narration
        assert await session.scalar(select(func.count()).select_from(VideoRender)) == 0
        assert await session.scalar(select(func.count()).select_from(NarrationAudio)) == 0
        assert await session.scalar(select(func.count()).select_from(ScenePlan)) == 0
        assert await session.scalar(select(func.count()).select_from(Scene)) == 0
        event = await session.scalar(
            select(ApprovalEvent).where(ApprovalEvent.action == "script_edited")
        )
        assert event is not None
        assert event.actor == "editor"
    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_scene_edit_invalidates_video_but_keeps_audio(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    async with seeded.sessions() as session:
        render = await session.get(VideoRender, seeded.video_id)
        from backend.app.db.models import NewsScript

        script = await session.get(NewsScript, seeded.script_id)
        await get_or_create_approval(session, render, script, seeded.settings)
        await session.commit()

        with pytest.raises(HTTPException) as error:
            await edit_scene(seeded.scene_id, SceneUpdate(subtitle="New subtitle"), session)
        assert error.value.status_code == 409

        result = await edit_scene(
            seeded.scene_id,
            SceneUpdate(
                subtitle="New subtitle",
                keywords=["verified", "response"],
                acknowledge_media_invalidation=True,
            ),
            session,
        )
        assert result.invalidated is True
        scene = await session.get(Scene, seeded.scene_id)
        assert scene.subtitle == "New subtitle"
        assert scene.keywords == ["verified", "response"]
        assert await session.scalar(select(func.count()).select_from(VideoRender)) == 0
        assert await session.scalar(select(func.count()).select_from(ContentApproval)) == 0
        assert await session.scalar(select(func.count()).select_from(NarrationAudio)) == 1
    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_review_list_detail_and_quality_checks(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    async with seeded.sessions() as session:
        summaries = await list_reviews(_fake_request(), session, None, 50)
        assert len(summaries) == 1
        assert summaries[0].approval_status == "pending"
        assert summaries[0].credibility_status == "corroborated"
        assert summaries[0].priority_score > 0

        detail = await get_review(_fake_request(), seeded.video_id, session)
        assert detail.summary.video_render_id == seeded.video_id
        assert detail.script.title == "Earthquake strikes city"
        assert detail.research is not None
        assert len(detail.research.sources) == 2
        assert detail.scene_plan.scenes[0].subtitle.startswith("Officials")
        assert detail.narration is not None
        assert checks_passed(detail.quality_checks)
        assert detail.approval.status == "pending"
    await seeded.engine.dispose()
