from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import httpx

from backend.app.publishers.contracts import PublishJob, PublishResult


def _slug(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return cleaned[:80] or fallback


class LocalExportPublisher:
    """Copy approved media into a local delivery folder with a metadata manifest.

    This provider makes the publishing pipeline testable and useful without any API
    credentials. It is explicitly reported as ``exported`` rather than ``published`` so
    local delivery is never confused with a public upload.
    """

    platform = "local"

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    async def publish(self, job: PublishJob) -> PublishResult:
        target_dir = self._output_dir / job.platform / job.video_render_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{_slug(job.title, job.video_render_id)}.mp4"
        await asyncio.to_thread(shutil.copy2, job.video_path, target)
        manifest = {
            "record_id": job.record_id,
            "video_render_id": job.video_render_id,
            "title": job.title,
            "description": job.description,
            "tags": job.tags,
            "duration_seconds": job.duration_seconds,
            "source_video": str(job.video_path),
            "exported_video": str(target),
            "approved_artifact_sha256": job.metadata.get("approved_artifact_sha256"),
            "platform": self.platform,
            "note": "Local export only; this is not a public social-media upload.",
        }
        (target_dir / "metadata.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return PublishResult(
            status="exported",
            external_id=str(target),
            external_url=target.resolve().as_uri(),
            metadata={"manifest_path": str(target_dir / "metadata.json")},
        )


class YouTubePublisher:
    """Official YouTube Data API resumable upload using a stored refresh token.

    The adapter uses the free YouTube Data API quota and requires the operator to create
    their own OAuth credentials. It never asks for a password and never logs tokens.
    """

    platform = "youtube"
    token_endpoint = "https://oauth2.googleapis.com/token"
    upload_endpoint = (
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status"
    )

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        privacy_status: str,
        category_id: str,
        default_language: str,
        made_for_kids: bool,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._privacy_status = privacy_status
        self._category_id = category_id
        self._default_language = default_language
        self._made_for_kids = made_for_kids

    @property
    def configured(self) -> bool:
        return all((self._client_id, self._client_secret, self._refresh_token))

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if not self.configured:
            raise RuntimeError(
                "YouTube publishing is not configured; set the YouTube OAuth environment variables"
            )
        response = await client.post(
            self.token_endpoint,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("YouTube OAuth did not return an access token")
        return token

    async def publish(self, job: PublishJob) -> PublishResult:
        if not job.video_path.is_file():
            raise FileNotFoundError(f"Approved video is missing: {job.video_path}")
        size = job.video_path.stat().st_size
        title = " ".join(job.title.split())[:100]
        description = job.description[:5_000]
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
            access_token = await self._access_token(client)
            session_response = await client.post(
                self.upload_endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Upload-Content-Length": str(size),
                    "X-Upload-Content-Type": "video/mp4",
                },
                json={
                    "snippet": {
                        "title": title,
                        "description": description,
                        "tags": job.tags[:30],
                        "categoryId": self._category_id,
                        "defaultLanguage": self._default_language,
                    },
                    "status": {
                        "privacyStatus": self._privacy_status,
                        "selfDeclaredMadeForKids": self._made_for_kids,
                    },
                },
            )
            session_response.raise_for_status()
            upload_url = session_response.headers.get("location")
            if not upload_url:
                raise RuntimeError("YouTube did not return a resumable upload URL")

            video_bytes = await asyncio.to_thread(job.video_path.read_bytes)
            upload_response = await client.put(
                upload_url,
                content=video_bytes,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(video_bytes)),
                },
            )
            upload_response.raise_for_status()
            payload = upload_response.json()
        video_id = payload.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise RuntimeError("YouTube upload response did not include a video id")
        return PublishResult(
            status="published",
            external_id=video_id,
            external_url=f"https://youtu.be/{video_id}",
            metadata={"privacy_status": self._privacy_status},
        )
