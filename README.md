# News Automation Platform

A local-first, modular news production pipeline. It collects public reporting, detects syndicated duplicates, assesses corroboration, builds an evidence brief, writes a neutral script, plans scenes, generates local narration, renders a draft video, and requires explicit human approval before any publishing action.

Nothing is uploaded automatically. Approval is a separate editorial decision from publishing, and publishing is pinned to the exact approved video artifact by SHA-256.

## Pipeline

```text
RSS collection
  -> duplicate clustering (syndicated reports never count as independent corroboration)
  -> credibility assessment and evidence trail
  -> research brief
  -> neutral script + automated review flags
  -> scene plan
  -> local narration (Piper)
  -> draft MP4 (FFmpeg)
  -> review dashboard + deterministic quality gate
  -> human approval (pinned artifact hash)
  -> local export and/or optional YouTube upload
```

Each stage writes a structured `AgentRun` audit record. Generated media is reproducible from stored source articles, briefs, scripts, and scene plans.

## Implemented phases

1. **Collection and storage** - configurable RSS sources, full-text extraction with `trafilatura`, normalization, language detection, idempotent ingestion, and audit logs.
2. **Deduplication** - local sentence-transformer embeddings, `confirmed` and human-review clusters, with all source articles retained.
3. **Credibility** - claim extraction, local corpus corroboration, trust-policy weighting, and conservative manual-review routing.
4. **Research** - extractive, source-linked briefs with explicit uncertainty; optional free GDELT context adapter.
5. **Script writing** - neutral scripts assembled only from corroborated research; sensational-language and minimum-source guardrails.
6. **Scene planning** - timed, editable scenes with subtitles and visual-search guidance.
7. **Text-to-speech** - local Piper synthesis with chunk timings and optional MP3 export.
8. **Video generation** - local FFmpeg render, muxed narration, and synchronized subtitle/timeline artifacts.
9. **Review and approval** - dashboard, quality gate, approval audit trail, safe script/scene editing, and downstream invalidation.
10. **Publishing** - explicit queue, hash enforcement, local export by default, and an optional official YouTube Data API adapter.

See [`docs/`](docs/) for the per-phase design notes.

## Approval and publishing safety

- Video renders start as `draft` and are never published by the rendering agent.
- The review API creates a `ContentApproval` record and runs deterministic checks before approval is allowed: the file exists and is large enough, subtitles contain timed cues, the timeline manifest is valid, duration and resolution are sane, and the source script passed its guardrails.
- Approval stores the SHA-256 hash of the exact MP4 that was reviewed.
- Queuing a publishing action requires `approval.status == "approved"`, `render.status == "approved"`, and an unchanged hash.
- The publishing agent re-checks both the approval hash and the hash pinned when the record was queued. If either changed, the record is blocked rather than uploaded.
- Approval is locked once publishing activity exists, so a queued or published artifact cannot be re-approved after modification.
- Publishing status (`queued`, `publishing`, `exported`, `published`, `failed`, `blocked`) is separate from editorial approval status.
- Every review decision and invalidation is recorded in `ApprovalEvent`, which is not deleted when media is regenerated.

## Quick start

Prerequisites:

- Python 3.11+
- FFmpeg on `PATH` (or set `NEWS_VIDEO_FFMPEG_EXECUTABLE`)
- Piper executable and voice model for narration (or configure an equivalent test path)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

Review `config/sources.yml` and `config/trust.yml`, then start the API:

```powershell
uvicorn backend.app.main:app --reload
```

Open the editorial dashboard at <http://127.0.0.1:8000/dashboard>. The API documentation is at <http://127.0.0.1:8000/docs>.

The dashboard loads the review queue, source/evidence provenance, script and scene editors, quality checks, approval controls, publishing records, agent runs, and the article library.

### Typical editorial run

1. Use **Advance pipeline** in the dashboard, or call the run endpoints in order: collection, deduplication, credibility, research, script writer, scene planner, TTS, video generator.
2. Open a rendered draft in **Review queue** and inspect sources, claims, evidence, script, scenes, and quality checks.
3. Approve, reject, or request changes. Rejection and change requests require notes.
4. Approved drafts can be queued for `local` export (no credentials required) or YouTube (if OAuth credentials are configured).
5. Use **Process queue** or `POST /api/v1/publishing/runs` to execute the approved queue. Queuing never uploads by itself.

Generated files default to `assets/generated/`; local publishing exports to `assets/published/`. Both directories are ignored by Git.

## Publishing configuration

The default provider is **local export**, which copies the approved MP4 and writes a metadata manifest without any third-party service.

YouTube support uses the official YouTube Data API resumable upload with a stored OAuth refresh token. It requires an operator-created Google Cloud OAuth client and the following environment variables:

```dotenv
NEWS_PUBLISHING_YOUTUBE_CLIENT_ID=
NEWS_PUBLISHING_YOUTUBE_CLIENT_SECRET=
NEWS_PUBLISHING_YOUTUBE_REFRESH_TOKEN=
NEWS_PUBLISHING_YOUTUBE_PRIVACY_STATUS=private
```

Credentials stay in `.env` or a secret manager. The adapter never requests a password and does not log tokens. Keep the dashboard and API bound to localhost unless you add authentication and TLS in front of them.

## API overview

| Area | Endpoints |
| --- | --- |
| Workflow triggers | `POST /api/v1/{collection,deduplication,credibility,research,script-writer,scene-planner,tts,video-generator}/runs` |
| Articles and provenance | `GET /api/v1/articles`, `/articles/{id}/credibility`, `/articles/{id}/research`, `/articles/{id}/script` |
| Review | `GET /api/v1/reviews`, `/reviews/analytics`, `/reviews/{video_render_id}`, `POST /reviews/{video_render_id}/decision`, `PATCH /reviews/scripts/{id}`, `PATCH /reviews/scenes/{id}`, `GET /reviews/videos/{id}/file` |
| Publishing | `POST /api/v1/publishing/queue`, `/publishing/runs`, `GET /publishing/records`, `/publishing/analytics`, `POST /publishing/records/{id}/retry` |
| Operations | `GET /api/v1/agent-runs`, `/approval-events`, `/system/status` |
| Dashboard | `GET /dashboard` and its static assets |

`/system/status` reports scheduler state, journal counts, and capability readiness for video FFmpeg, TTS FFmpeg, Piper, local embeddings, local publishing, and YouTube credentials.

## Configuration

Configuration uses the `NEWS_` environment prefix and is documented in [`.env.example`](.env.example). The most important settings are:

- `NEWS_DATABASE_URL` - defaults to SQLite; any async SQLAlchemy URL is supported.
- `NEWS_SOURCES_FILE` and `NEWS_TRUST_POLICY_FILE` - editorial source and trust configuration.
- `NEWS_*_ENABLED` and `NEWS_*_INTERVAL_MINUTES` - scheduler control per agent.
- `NEWS_APPROVAL_MIN_VIDEO_BYTES` - minimum artifact size accepted by the quality gate.
- `NEWS_PUBLISHING_DEFAULT_PLATFORM`, `NEWS_PUBLISHING_OUTPUT_DIR`, and the YouTube variables above.

## Quality checks

```powershell
$env:TEMP='C:\Users\Shiva\AppData\Local\Temp\opencode'
$env:TMP=$env:TEMP
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
```

The temporary-directory override is only needed on Windows installations where the default pytest temp directory has restrictive ACLs.

## Current limitations

- The automated tests use fake video and narration renderers. Real FFmpeg rendering and real Piper synthesis require those binaries and models to be installed and were not verifiable in the development environment used for this repository.
- `sentence-transformers` is declared as a dependency, but semantic deduplication needs the model download or a local model directory before its first real run.
- The dashboard has no built-in login. It is designed for local or single-operator use behind a trusted network boundary.
- The schema is created with SQLAlchemy `create_all`; there is no migration framework yet.
- The optional YouTube adapter performs the upload request after recording an in-flight `publishing` attempt. If the process dies after the remote upload but before the result commit, reconcile the record manually before retrying to avoid a duplicate upload.
