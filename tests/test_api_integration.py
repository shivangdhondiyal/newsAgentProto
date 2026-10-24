from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.agents.credibility.trust import SourceTrustPolicy
from backend.app.api import publishing, reviews, system
from backend.app.publishers.agent import PublishingAgent
from tests.support import seed_content


def _api_app(seeded) -> FastAPI:
    app = FastAPI()
    app.state.session_factory = seeded.sessions
    app.state.settings = seeded.settings
    app.state.trust_policy = SourceTrustPolicy.from_file(Path("config/trust.yml"))
    app.state.publishing_agent = PublishingAgent(seeded.settings, seeded.sessions)
    app.include_router(reviews.router, prefix="/api/v1")
    app.include_router(publishing.router, prefix="/api/v1")
    app.include_router(system.router, prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_review_approval_and_local_publishing_over_http(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    app = _api_app(seeded)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        queue = await client.get("/api/v1/reviews")
        assert queue.status_code == 200
        items = queue.json()
        assert len(items) == 1
        assert items[0]["approval_status"] == "pending"
        video_id = items[0]["video_render_id"]

        detail = await client.get(f"/api/v1/reviews/{video_id}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["article"]["title"] == "Earthquake strikes city"
        assert payload["research"]["source_count"] == 2
        assert payload["quality_checks"]
        assert all(check["status"] != "failed" for check in payload["quality_checks"])

        stream = await client.get(f"/api/v1/reviews/videos/{video_id}/file")
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("video/mp4")
        assert len(stream.content) >= 4_096

        decision = await client.post(
            f"/api/v1/reviews/{video_id}/decision",
            json={
                "action": "approve",
                "reviewer": "integration-editor",
                "notes": "Provenance and quality checks verified.",
            },
        )
        assert decision.status_code == 200
        approved = decision.json()
        assert approved["status"] == "approved"
        assert approved["artifact_sha256"]

        queued = await client.post(
            "/api/v1/publishing/queue",
            json={"video_render_id": video_id, "platform": "local"},
        )
        assert queued.status_code == 201
        record = queued.json()
        assert record["status"] == "queued"
        assert record["metadata_json"]["approved_artifact_sha256"] == approved["artifact_sha256"]

        duplicate = await client.post(
            "/api/v1/publishing/queue",
            json={"video_render_id": video_id, "platform": "local"},
        )
        assert duplicate.status_code == 409

        locked_decision = await client.post(
            f"/api/v1/reviews/{video_id}/decision",
            json={"action": "reject", "reviewer": "integration-editor", "notes": "Changed mind."},
        )
        assert locked_decision.status_code == 409

        locked_script = await client.patch(
            f"/api/v1/reviews/scripts/{seeded.script_id}",
            json={
                "title": "A different headline",
                "acknowledge_media_invalidation": True,
                "actor": "integration-editor",
            },
        )
        assert locked_script.status_code == 409

        run = await client.post("/api/v1/publishing/runs")
        assert run.status_code == 200
        assert run.json()["status"] == "completed"
        assert run.json()["details"]["exported"] == 1

        records = await client.get("/api/v1/publishing/records")
        assert records.status_code == 200
        exported = records.json()
        assert exported[0]["status"] == "exported"
        assert Path(exported[0]["external_id"]).is_file()

        analytics = await client.get("/api/v1/publishing/analytics")
        assert analytics.status_code == 200
        assert analytics.json()["by_status"] == {"exported": 1}
        assert analytics.json()["external_publish_count"] == 0

        review_analytics = await client.get("/api/v1/reviews/analytics")
        assert review_analytics.status_code == 200
        assert review_analytics.json()["approved"] == 1
        assert review_analytics.json()["exported"] == 1

        status = await client.get("/api/v1/system/status")
        assert status.status_code == 200
        journal = status.json()["journal"]
        assert journal["articles"] == 1
        assert journal["video_renders"] == 1
        assert journal["pending_approvals"] == 0
        assert journal["publishing_records"] == 1
        capabilities = status.json()["capabilities"]
        assert capabilities["local_publishing"] is True
        assert set(capabilities) == {
            "video_ffmpeg",
            "tts_ffmpeg",
            "piper_tts",
            "local_embeddings",
            "local_publishing",
            "youtube_publishing",
        }

        runs = await client.get("/api/v1/agent-runs?agent_name=publishing")
        assert runs.status_code == 200
        assert len(runs.json()) == 1

        events = await client.get("/api/v1/approval-events")
        assert events.status_code == 200
        assert "approved" in {event["action"] for event in events.json()}

    await seeded.engine.dispose()
