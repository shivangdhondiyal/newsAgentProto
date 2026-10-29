from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import HTTPException

from backend.app.api.publishing import (
    download_local_export,
    publishing_analytics,
    queue_publish,
)
from backend.app.db.models import ContentApproval, NewsScript, PublishingRecord, VideoRender
from backend.app.publishers.agent import PublishingAgent
from backend.app.publishers.contracts import PublishJob
from backend.app.publishers.providers import YouTubePublisher
from backend.app.reviews.service import decide_approval, get_or_create_approval
from backend.app.schemas.publishing import PublishingQueueRequest
from tests.support import seed_content


async def _approve(seeded) -> None:
    async with seeded.sessions() as session:
        render = await session.get(VideoRender, seeded.video_id)
        script = await session.get(NewsScript, seeded.script_id)
        approval = await get_or_create_approval(session, render, script, seeded.settings)
        await decide_approval(
            session,
            render=render,
            script=script,
            approval=approval,
            action="approve",
            reviewer="editor",
            notes="Approved for publishing tests.",
            settings=seeded.settings,
        )
        await session.commit()


def _request(agent: PublishingAgent) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(publishing_agent=agent)))


@pytest.mark.asyncio
async def test_unapproved_content_cannot_be_queued(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    agent = PublishingAgent(seeded.settings, seeded.sessions)
    async with seeded.sessions() as session:
        with pytest.raises(HTTPException) as error:
            await queue_publish(
                _request(agent),
                PublishingQueueRequest(video_render_id=seeded.video_id, platform="local"),
                session,
            )
        assert error.value.status_code == 409
    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_approved_local_export_is_idempotent_and_auditable(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    await _approve(seeded)
    agent = PublishingAgent(seeded.settings, seeded.sessions)
    request = _request(agent)

    async with seeded.sessions() as session:
        record = await queue_publish(
            request,
            PublishingQueueRequest(video_render_id=seeded.video_id, platform="local"),
            session,
        )
        assert record.status == "queued"
        assert record.title == "Earthquake strikes city"
        assert "Sources:" in record.description
        assert "https://reuters.example/report" in record.description
        record_id = record.id
        with pytest.raises(HTTPException) as error:
            await queue_publish(
                request,
                PublishingQueueRequest(video_render_id=seeded.video_id, platform="local"),
                session,
            )
        assert error.value.status_code == 409

    run = await agent.execute()
    assert run.status == "completed"
    assert run.items_created == 1

    async with seeded.sessions() as session:
        record = await session.get(PublishingRecord, record_id)
        assert record.status == "exported"
        assert record.external_id is not None
        assert Path(record.external_id).is_file()
        assert record.metadata_json["manifest_path"]
        analytics = await publishing_analytics(session, days=30)
        assert analytics.by_status == {"exported": 1}
        assert analytics.by_platform == {"local": 1}
        assert analytics.external_publish_count == 0
        assert analytics.publishing_success_rate == 100.0
    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_artifact_change_after_approval_blocks_publishing(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    await _approve(seeded)
    agent = PublishingAgent(seeded.settings, seeded.sessions)
    async with seeded.sessions() as session:
        record = await queue_publish(
            _request(agent),
            PublishingQueueRequest(video_render_id=seeded.video_id, platform="local"),
            session,
        )
        record_id = record.id

    seeded.video_path.write_bytes(b"tampered-after-approval")
    run = await agent.execute()
    assert run.status == "completed_with_errors"
    async with seeded.sessions() as session:
        record = await session.get(PublishingRecord, record_id)
        assert record.status == "blocked"
        assert "changed after approval" in record.last_error
    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_reapproval_after_artifact_change_cannot_reuse_a_queued_record(
    tmp_path: Path,
) -> None:
    seeded = await seed_content(tmp_path)
    await _approve(seeded)
    agent = PublishingAgent(seeded.settings, seeded.sessions)
    async with seeded.sessions() as session:
        record = await queue_publish(
            _request(agent),
            PublishingQueueRequest(video_render_id=seeded.video_id, platform="local"),
            session,
        )
        record_id = record.id

    # Simulate another caller re-approving a modified file. The approval hash moves, but
    # the publishing record stays pinned to the hash that was reviewed when it was queued.
    seeded.video_path.write_bytes(b"replacement-video" * 300)
    async with seeded.sessions() as session:
        render = await session.get(VideoRender, seeded.video_id)
        script = await session.get(NewsScript, seeded.script_id)
        approval = await session.get(ContentApproval, record.approval_id)
        await decide_approval(
            session,
            render=render,
            script=script,
            approval=approval,
            action="approve",
            reviewer="second-editor",
            notes="Re-approved modified artifact.",
            settings=seeded.settings,
        )
        await session.commit()

    run = await agent.execute()
    assert run.status == "completed_with_errors"
    async with seeded.sessions() as session:
        record = await session.get(PublishingRecord, record_id)
        assert record.status == "blocked"
        assert "pinned when it was queued" in record.last_error
    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_local_export_file_is_served_only_from_the_publish_root(
    tmp_path: Path,
) -> None:
    seeded = await seed_content(tmp_path)
    await _approve(seeded)
    agent = PublishingAgent(seeded.settings, seeded.sessions)
    async with seeded.sessions() as session:
        record = await queue_publish(
            _request(agent),
            PublishingQueueRequest(video_render_id=seeded.video_id, platform="local"),
            session,
        )
        record_id = record.id
    await agent.execute()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=seeded.settings)))
    publish_root = Path(seeded.settings.publishing_output_dir).resolve()

    async with seeded.sessions() as session:
        response = await download_local_export(request, record_id, session)
        exported = Path(response.path).resolve()
        assert exported.is_file()
        assert exported.is_relative_to(publish_root)
        assert response.media_type == "video/mp4"

        with pytest.raises(HTTPException) as missing:
            await download_local_export(request, "missing-record", session)
        assert missing.value.status_code == 404

        record = await session.get(PublishingRecord, record_id)
        record.status = "queued"
        await session.commit()
        with pytest.raises(HTTPException) as incomplete:
            await download_local_export(request, record_id, session)
        assert incomplete.value.status_code == 409

        record.status = "exported"
        record.external_url = seeded.video_path.resolve().as_uri()
        await session.commit()
        with pytest.raises(HTTPException) as outside:
            await download_local_export(request, record_id, session)
        assert outside.value.status_code == 404

    await seeded.engine.dispose()


@respx.mock
@pytest.mark.asyncio
async def test_youtube_provider_uses_official_resumable_upload(tmp_path: Path) -> None:
    video_path = tmp_path / "draft.mp4"
    video_path.write_bytes(b"video-bytes")
    token_route = respx.post(YouTubePublisher.token_endpoint).mock(
        return_value=httpx.Response(200, json={"access_token": "test-token"})
    )
    init_route = respx.route(
        method="POST",
        url__startswith="https://www.googleapis.com/upload/youtube/v3/videos",
    ).mock(return_value=httpx.Response(200, headers={"location": "https://upload.example/session"}))
    upload_route = respx.put("https://upload.example/session").mock(
        return_value=httpx.Response(200, json={"id": "video123"})
    )
    provider = YouTubePublisher(
        client_id="client",
        client_secret="secret",
        refresh_token="refresh",
        privacy_status="private",
        category_id="25",
        default_language="en",
        made_for_kids=False,
    )
    result = await provider.publish(
        PublishJob(
            record_id="record",
            video_render_id="render",
            platform="youtube",
            title="A neutral headline",
            description="Description with cited sources.",
            tags=["news", "verified"],
            video_path=video_path,
            duration_seconds=2.5,
        )
    )

    assert result.status == "published"
    assert result.external_id == "video123"
    assert result.external_url == "https://youtu.be/video123"
    assert token_route.called
    assert init_route.called
    assert upload_route.called
    upload_request = init_route.calls.last.request
    assert upload_request.headers["Authorization"] == "Bearer test-token"
    assert '"privacyStatus":"private"' in upload_request.content.decode().replace(" ", "")


@pytest.mark.asyncio
async def test_youtube_provider_requires_credentials(tmp_path: Path) -> None:
    video_path = tmp_path / "draft.mp4"
    video_path.write_bytes(b"video-bytes")
    provider = YouTubePublisher(
        client_id="",
        client_secret="",
        refresh_token="",
        privacy_status="private",
        category_id="25",
        default_language="en",
        made_for_kids=False,
    )
    with pytest.raises(RuntimeError, match="not configured"):
        await provider.publish(
            PublishJob(
                record_id="record",
                video_render_id="render",
                platform="youtube",
                title="Title",
                description="Description",
                tags=[],
                video_path=video_path,
                duration_seconds=1.0,
            )
        )
