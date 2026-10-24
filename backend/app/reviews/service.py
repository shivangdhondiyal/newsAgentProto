from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import Settings
from backend.app.db.models import (
    ApprovalEvent,
    ContentApproval,
    NarrationAudio,
    NarrationChunk,
    NewsScript,
    PublishingRecord,
    Scene,
    ScenePlan,
    VideoRender,
)
from backend.app.reviews.quality import checks_passed, evaluate_video_quality, sha256_file


class ApprovalBlockedError(Exception):
    """Raised when deterministic quality checks fail before approval."""

    def __init__(self, checks: list[dict[str, Any]]) -> None:
        self.checks = checks
        super().__init__("Video quality checks failed; approval was not recorded")


async def get_or_create_approval(
    session: AsyncSession,
    render: VideoRender,
    script: NewsScript | None,
    settings: Settings,
) -> ContentApproval:
    approval = await session.scalar(
        select(ContentApproval).where(ContentApproval.video_render_id == render.id)
    )
    if approval is not None:
        return approval

    checks = evaluate_video_quality(render, script, settings.approval_min_video_bytes)
    approval = ContentApproval(
        video_render_id=render.id,
        status="pending",
        quality_checks=checks,
        artifact_sha256=sha256_file(Path(render.video_path)),
    )
    session.add(approval)
    await session.flush()
    return approval


def record_event(
    session: AsyncSession,
    *,
    video_render_id: str | None,
    script_id: str | None,
    action: str,
    actor: str,
    notes: str | None,
) -> None:
    session.add(
        ApprovalEvent(
            video_render_id=video_render_id,
            script_id=script_id,
            action=action,
            actor=actor or "operator",
            notes=notes,
        )
    )


async def decide_approval(
    session: AsyncSession,
    *,
    render: VideoRender,
    script: NewsScript | None,
    approval: ContentApproval,
    action: str,
    reviewer: str,
    notes: str | None,
    settings: Settings,
) -> ContentApproval:
    """Apply a human decision while enforcing the quality gate for approvals."""

    if action == "approve":
        checks = evaluate_video_quality(render, script, settings.approval_min_video_bytes)
        approval.quality_checks = checks
        if not checks_passed(checks):
            raise ApprovalBlockedError(checks)
        artifact_hash = sha256_file(Path(render.video_path))
        if artifact_hash is None:
            raise ApprovalBlockedError(checks)
        approval.status = "approved"
        approval.artifact_sha256 = artifact_hash
        render.status = "approved"
        event_action = "approved"
    elif action == "reject":
        approval.status = "rejected"
        render.status = "rejected"
        event_action = "rejected"
    elif action == "request_changes":
        approval.status = "changes_requested"
        render.status = "changes_requested"
        event_action = "changes_requested"
    else:
        raise ValueError(f"Unsupported review action: {action}")

    approval.reviewer = reviewer
    approval.notes = notes
    approval.decided_at = datetime.now(UTC)
    record_event(
        session,
        video_render_id=render.id,
        script_id=script.id if script else None,
        action=event_action,
        actor=reviewer,
        notes=notes,
    )
    await session.flush()
    return approval


def _safe_remove_tree(path: Path, root: Path) -> None:
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        return
    shutil.rmtree(resolved_path, ignore_errors=True)


async def remove_video_generation(
    session: AsyncSession,
    *,
    render: VideoRender,
    settings: Settings,
    actor: str,
    notes: str | None,
    action: str = "media_invalidated",
    script_id: str | None = None,
) -> None:
    """Remove a generated video and its approval before a replacement is rendered."""

    approval = await session.scalar(
        select(ContentApproval).where(ContentApproval.video_render_id == render.id)
    )
    record_event(
        session,
        video_render_id=render.id,
        script_id=script_id,
        action=action,
        actor=actor,
        notes=notes,
    )
    if approval is not None:
        await session.delete(approval)
    await session.delete(render)
    await session.flush()
    _safe_remove_tree(Path(render.video_path).parent, settings.video_output_dir)


async def remove_script_generation(
    session: AsyncSession,
    *,
    script: NewsScript,
    settings: Settings,
    actor: str,
    notes: str | None,
    action: str = "script_edited",
) -> None:
    """Invalidate downstream generated media after an acknowledged script edit."""

    publishing = await session.scalar(
        select(PublishingRecord).where(
            PublishingRecord.video_render_id.in_(
                select(VideoRender.id)
                .join(NarrationAudio, NarrationAudio.id == VideoRender.narration_audio_id)
                .where(NarrationAudio.script_id == script.id)
            )
        )
    )
    if publishing is not None:
        raise ValueError("Published or queued content cannot be regenerated")

    render_ids = list(
        (
            await session.scalars(
                select(VideoRender.id)
                .join(NarrationAudio, NarrationAudio.id == VideoRender.narration_audio_id)
                .where(NarrationAudio.script_id == script.id)
            )
        ).all()
    )
    if render_ids:
        for render_id in render_ids:
            record_event(
                session,
                video_render_id=render_id,
                script_id=script.id,
                action=action,
                actor=actor,
                notes=notes,
            )
    else:
        record_event(
            session,
            video_render_id=None,
            script_id=script.id,
            action=action,
            actor=actor,
            notes=notes,
        )

    narrations = list(
        (
            await session.scalars(
                select(NarrationAudio).where(NarrationAudio.script_id == script.id)
            )
        ).all()
    )
    for narration in narrations:
        renders = list(
            (
                await session.scalars(
                    select(VideoRender).where(VideoRender.narration_audio_id == narration.id)
                )
            ).all()
        )
        for render in renders:
            approval = await session.scalar(
                select(ContentApproval).where(ContentApproval.video_render_id == render.id)
            )
            if approval is not None:
                await session.delete(approval)
            _safe_remove_tree(Path(render.video_path).parent, settings.video_output_dir)
            await session.delete(render)
        await session.execute(
            delete(NarrationChunk).where(NarrationChunk.narration_audio_id == narration.id)
        )
        await session.delete(narration)

    plans = list(
        (await session.scalars(select(ScenePlan).where(ScenePlan.script_id == script.id))).all()
    )
    for plan in plans:
        await session.execute(delete(Scene).where(Scene.scene_plan_id == plan.id))
        await session.delete(plan)

    _safe_remove_tree(settings.tts_output_dir / script.id, settings.tts_output_dir)
    await session.flush()
