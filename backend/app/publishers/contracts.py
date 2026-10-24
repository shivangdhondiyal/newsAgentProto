from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class PublishJob:
    record_id: str
    video_render_id: str
    platform: str
    title: str
    description: str
    tags: list[str]
    video_path: Path
    duration_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PublishResult:
    status: str
    external_id: str | None = None
    external_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PublishProvider(Protocol):
    platform: str

    async def publish(self, job: PublishJob) -> PublishResult: ...
