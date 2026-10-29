from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.api import captions
from backend.app.core.config import Settings
from backend.app.db.models import ProductionJob, ProductionStage
from backend.app.services.captions import CaptionService
from backend.app.services.production import ProductionNotFound
from tests.support import seed_content


class _StubProduction:
    """Re-render the persisted job without spawning the real background runner."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def rerender_job(self, job_id: str) -> ProductionJob:
        async with self._session_factory() as session:
            job = await session.get(ProductionJob, job_id)
            if job is None:
                raise ProductionNotFound("Production job not found")
            job.status = "running"
            await session.commit()
            return job


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        caption_output_dir=tmp_path / "captions",
        caption_transcription_provider="none",
        caption_burn_in_default=True,
        caption_soft_subs_default=True,
        caption_sidecar_default=True,
    )


async def _seed_job(seeded) -> str:
    async with seeded.sessions() as session:
        job = ProductionJob(
            script_id=seeded.script_id,
            article_id=seeded.article_id,
            status="completed",
            style_key="broadcast",
            aspect_ratio="both",
            fingerprint="caption-api-test",
            requested_by="pytest",
            config_json={"captions": True},
            logs=[],
            warnings=[],
        )
        session.add(job)
        await session.flush()
        session.add_all(
            [
                ProductionStage(
                    job_id=job.id,
                    key="render",
                    label="Render 16:9 + 9:16",
                    status="completed",
                    attempts=1,
                    logs=[],
                ),
                ProductionStage(
                    job_id=job.id,
                    key="verify",
                    label="Verify outputs",
                    status="completed",
                    attempts=1,
                    logs=[],
                ),
            ]
        )
        await session.commit()
        return job.id


def _app(seeded, settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.session_factory = seeded.sessions
    app.state.settings = settings
    app.state.caption_service = CaptionService(settings, seeded.sessions)
    app.state.production_service = _StubProduction(seeded.sessions)
    app.include_router(captions.router, prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_caption_api_generate_edit_export_import_and_rerender(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    settings = _settings(tmp_path)
    job_id = await _seed_job(seeded)
    app = _app(seeded, settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        config = await client.get("/api/v1/captions/config")
        assert config.status_code == 200
        body = config.json()
        assert len(body["presets"]) == 6
        assert body["defaults"]["preset_key"] == "clean_editorial"
        assert body["fonts"]
        assert "not_generated" in body["statuses"]

        before = await client.get(f"/api/v1/captions/jobs/{job_id}")
        assert before.status_code == 200
        assert before.json()["track"] is None
        assert before.json()["can_generate"] is True

        generated = await client.post(
            f"/api/v1/captions/jobs/{job_id}/generate",
            json={"preset_key": "clean_editorial"},
        )
        assert generated.status_code == 201
        track = generated.json()
        assert track["status"] == "ready"
        assert track["source"] == "narration_alignment"
        assert track["cues"]
        assert track["srt_url"] == f"/api/v1/captions/tracks/{track['id']}/export/srt"
        assert track["ass_urls"]["9:16"].endswith("aspect=9:16")
        linked_srt = await client.get(track["srt_url"])
        assert linked_srt.status_code == 200
        linked_vtt = await client.get(track["vtt_url"])
        assert linked_vtt.status_code == 200
        linked_ass = await client.get(track["ass_urls"]["9:16"])
        assert linked_ass.status_code == 200
        track_id = track["id"]

        after = await client.get(f"/api/v1/captions/jobs/{job_id}")
        assert after.status_code == 200
        assert after.json()["track"]["id"] == track_id

        cues = track["cues"]
        cues[0]["text"] = "Edited opening caption."
        cues[0]["end"] = max(cues[0]["start"] + 0.4, cues[0]["end"])
        edited = await client.put(
            f"/api/v1/captions/tracks/{track_id}/cues",
            json={"cues": cues},
        )
        assert edited.status_code == 200
        assert edited.json()["status"] == "edited"
        assert edited.json()["edited"] is True
        assert edited.json()["cues"][0]["text"] == "Edited opening caption."
        # Word timings must be re-derived when the text no longer matches them.
        words = edited.json()["cues"][0]["words"]
        assert " ".join(word["text"] for word in words) == "Edited opening caption."
        assert all(
            edited.json()["cues"][0]["start"] <= word["start"] <= word["end"] for word in words
        )
        assert words[-1]["end"] <= edited.json()["cues"][0]["end"] + 0.001

        styled = await client.patch(
            f"/api/v1/captions/tracks/{track_id}/settings",
            json={
                "preset_key": "professional_news",
                "settings": {
                    **edited.json()["settings"],
                    "preset_key": "professional_news",
                    "background_opacity": 0.6,
                    "landscape": {
                        **edited.json()["settings"]["landscape"],
                        "font_size": 50,
                    },
                },
            },
        )
        assert styled.status_code == 200
        assert styled.json()["preset_key"] == "professional_news"
        assert styled.json()["settings"]["landscape"]["font_size"] == 50

        srt = await client.get(f"/api/v1/captions/tracks/{track_id}/export/srt")
        assert srt.status_code == 200
        assert "Edited opening caption." in srt.text
        vtt = await client.get(f"/api/v1/captions/tracks/{track_id}/export/vtt")
        assert vtt.status_code == 200
        assert vtt.text.startswith("WEBVTT")
        ass = await client.get(f"/api/v1/captions/tracks/{track_id}/export/ass?aspect=9:16")
        assert ass.status_code == 200
        assert "PlayResX: 1080" in ass.text

        imported = await client.post(
            f"/api/v1/captions/tracks/{track_id}/import",
            json={
                "format": "srt",
                "content": (
                    "1\n00:00:00,200 --> 00:00:01,200\nImported line.\n\n"
                    "2\n00:00:01,400 --> 00:00:02,200\nSecond imported line.\n"
                ),
            },
        )
        assert imported.status_code == 200
        assert imported.json()["source"] == "imported"
        assert imported.json()["cues"][0]["text"] == "Imported line."

        blocked = await client.post(
            f"/api/v1/captions/tracks/{track_id}/regenerate",
            json={"force": False},
        )
        assert blocked.status_code == 409

        preserved = await client.post(
            f"/api/v1/captions/tracks/{track_id}/regenerate",
            json={"force": True},
        )
        assert preserved.status_code == 200
        # Regenerating on the same preset keeps the customised look.
        assert preserved.json()["preset_key"] == "professional_news"
        assert preserved.json()["settings"]["background_opacity"] == 0.6
        assert preserved.json()["settings"]["landscape"]["font_size"] == 50

        regenerated = await client.post(
            f"/api/v1/captions/tracks/{track_id}/regenerate",
            json={"force": True, "preset_key": "clean_editorial"},
        )
        assert regenerated.status_code == 200
        assert regenerated.json()["edited"] is False
        assert regenerated.json()["status"] == "ready"
        # Switching preset must restore that preset's look, not the old one.
        assert regenerated.json()["preset_key"] == "clean_editorial"
        assert regenerated.json()["settings"]["font_family"] == "Segoe UI"
        assert regenerated.json()["settings"]["alignment"] == "center"
        assert regenerated.json()["settings"]["landscape"]["font_size"] == 46

        rerendered = await client.post(f"/api/v1/captions/jobs/{job_id}/rerender")
        assert rerendered.status_code == 202
        payload = rerendered.json()
        assert payload["status"] == "running"
        assert payload["caption_track"]["id"] == track_id

    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_caption_api_rejects_generation_without_narration(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    settings = _settings(tmp_path)
    job_id = await _seed_job(seeded)
    app = _app(seeded, settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Remove the narration so the endpoint must explain what is missing.
        from sqlalchemy import delete

        from backend.app.db.models import NarrationAudio, NarrationChunk

        async with seeded.sessions() as session:
            await session.execute(delete(NarrationChunk))
            await session.execute(delete(NarrationAudio))
            await session.commit()

        response = await client.post(f"/api/v1/captions/jobs/{job_id}/generate", json={})
        assert response.status_code == 409
        assert "Narration is not ready" in response.json()["detail"]

        job = await client.get(f"/api/v1/captions/jobs/{job_id}")
        assert job.status_code == 200
        assert job.json()["can_generate"] is False
        assert job.json()["blocking_reason"]

    await seeded.engine.dispose()


@pytest.mark.asyncio
async def test_caption_api_returns_actionable_errors(tmp_path: Path) -> None:
    seeded = await seed_content(tmp_path)
    settings = _settings(tmp_path)
    job_id = await _seed_job(seeded)
    app = _app(seeded, settings)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/api/v1/captions/tracks/does-not-exist")
        assert missing.status_code == 404
        assert "not found" in missing.json()["detail"]

        generated = await client.post(f"/api/v1/captions/jobs/{job_id}/generate", json={})
        track_id = generated.json()["id"]

        bad_import = await client.post(
            f"/api/v1/captions/tracks/{track_id}/import",
            json={"format": "srt", "content": "not a subtitle"},
        )
        assert bad_import.status_code == 422
        assert "No readable cues" in bad_import.json()["detail"]

        bad_cues = await client.put(
            f"/api/v1/captions/tracks/{track_id}/cues",
            json={"cues": []},
        )
        assert bad_cues.status_code == 422

    await seeded.engine.dispose()
