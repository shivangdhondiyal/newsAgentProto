from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from backend.app.db.models import NewsScript, VideoRender


def sha256_file(path: Path) -> str | None:
    """Return a stable artifact fingerprint without loading large videos into memory."""

    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def evaluate_video_quality(
    render: VideoRender, script: NewsScript | None, minimum_bytes: int = 1024
) -> list[dict[str, Any]]:
    """Run deterministic local checks before a human is allowed to approve a render.

    These checks catch missing or corrupt artifacts, stale subtitle/timeline metadata, and
    scripts that never passed the automated review gate. They do not substitute for
    editorial review of accuracy or fairness.
    """

    checks: list[dict[str, Any]] = []
    video_path = Path(render.video_path)
    if not video_path.is_file():
        checks.append(_check("video_file", "failed", f"Video file not found: {video_path}"))
    else:
        size = video_path.stat().st_size
        if size < minimum_bytes:
            checks.append(
                _check(
                    "video_file",
                    "failed",
                    f"Video file is only {size} bytes; at least {minimum_bytes} expected",
                )
            )
        else:
            checks.append(
                _check("video_file", "passed", f"Video file present ({size:,} bytes)")
            )

    subtitle_path = Path(render.subtitle_path)
    if not subtitle_path.is_file() or "-->" not in subtitle_path.read_text(
        encoding="utf-8", errors="replace"
    ):
        checks.append(
            _check("subtitles", "failed", f"Missing or invalid subtitle track: {subtitle_path}")
        )
    else:
        checks.append(_check("subtitles", "passed", "Subtitle track contains timed cues"))

    timeline_path = Path(render.timeline_path)
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(_check("timeline", "failed", f"Timeline manifest is invalid: {exc}"))
    else:
        if not isinstance(timeline, list) or not timeline:
            checks.append(_check("timeline", "failed", "Timeline manifest has no scenes"))
        else:
            checks.append(
                _check("timeline", "passed", f"Timeline manifest contains {len(timeline)} scenes")
            )

    if render.duration_seconds <= 0:
        checks.append(_check("duration", "failed", "Video duration must be positive"))
    elif render.duration_seconds > 3_600:
        checks.append(
            _check(
                "duration",
                "warning",
                f"Video is unusually long ({render.duration_seconds:.1f}s)",
            )
        )
    else:
        checks.append(
            _check("duration", "passed", f"Duration recorded as {render.duration_seconds:.2f}s")
        )

    if render.width <= 0 or render.height <= 0:
        checks.append(_check("resolution", "failed", "Video width and height must be positive"))
    elif render.width % 2 or render.height % 2:
        checks.append(
            _check("resolution", "failed", "H.264 output requires even width and height")
        )
    else:
        checks.append(
            _check("resolution", "passed", f"{render.width}x{render.height} at {render.fps} fps")
        )

    if script is None:
        checks.append(_check("script_gate", "failed", "Source script could not be found"))
    elif script.review_reasons or script.status != "draft":
        checks.append(
            _check(
                "script_gate",
                "failed",
                "Source script is still flagged for review and is not eligible for approval",
            )
        )
    else:
        checks.append(_check("script_gate", "passed", "Source script passed automated guardrails"))

    return checks


def checks_passed(checks: list[dict[str, Any]]) -> bool:
    return not any(check.get("status") == "failed" for check in checks)
