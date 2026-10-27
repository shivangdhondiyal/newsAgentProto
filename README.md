# News Automation Platform

A local-first, modular news production pipeline. It collects public reporting, detects syndicated duplicates, assesses corroboration, builds an evidence brief, writes a neutral script, plans scenes, generates local narration, renders a draft video, and requires explicit human approval before any publishing action.

Nothing is uploaded automatically. Approval is a separate editorial decision from publishing, and publishing is pinned to the exact approved video artifact by SHA-256.

## Pipeline

```text
RSS collection
  -> duplicate clustering + publisher grouping
     (syndicated copies and multiple feeds from one publisher never count as independent corroboration)
  -> credibility assessment and evidence trail
  -> research brief
  -> neutral script + automated review flags
  -> scene plan (narration-aligned timing)
  -> licensed visual retrieval + designed fallbacks
  -> local narration (Piper) with loudness mastering
  -> 16:9 and 9:16 draft MP4s (FFmpeg + Pillow scene graphics)
  -> review dashboard + deterministic quality gate
  -> human approval (pinned artifact hash)
  -> local export and/or optional YouTube upload
```

Each stage writes a structured `AgentRun` audit record. Generated media is reproducible from stored source articles, briefs, scripts, and scene plans.

## Implemented phases

1. **Collection and storage** - six configurable publishers by default (BBC World/Technology, Guardian World, NPR World, France 24, CBC World), full-text extraction with `trafilatura`, markup and extraction-artifact cleanup, normalization, language detection, idempotent ingestion, and audit logs.
2. **Deduplication** - local sentence-transformer embeddings, `confirmed` and human-review clusters, with all source articles retained.
3. **Credibility** - claim extraction, publisher-aware local corpus corroboration, trust-policy weighting, and conservative manual-review routing.
4. **Research** - extractive, source-linked briefs with explicit uncertainty; optional free GDELT context adapter.
5. **Script writing** - neutral scripts assembled only from corroborated research; sensational-language and minimum-source guardrails.
6. **Scene planning** - timed, editable scenes with subtitles, searchable subjects derived from the narration, and narration-aligned timing that keeps pauses with the outgoing scene.
7. **Visual assets** - licensed retrieval from Wikimedia Commons and NASA (plus optional Pexels/Pixabay keys), with signature and decode validation, normalized caching, focal-point crops, relevance ranking, and stored creator/license/source provenance. A scene never falls back to an empty frame: it gets a labelled, style-aware designed graphic.
8. **Text-to-speech** - local Piper synthesis with paragraph/sentence-aware chunking, natural pauses, retry, and a two-pass EBU R128 mastering chain (EQ, compression, limiter, fades, optional ducked licensed music). Raw audio is retained if mastering fails.
9. **Video generation** - one job renders separate 16:9 and 9:16 compositions from the same timeline, with style-specific typography, palettes, transitions, motion, captions, and safe areas. VOX and data styles use generated maps, charts, timelines, stat cards, quote cards, and diagrams rather than zoomed stock photos.
10. **Production orchestration** - a persisted six-stage job (`prepare`, `scenes`, `assets`, `narrate`, `render`, `verify`) with preflight configuration, duplicate suppression, stage logs, resume/retry, and downstream invalidation when narration is regenerated.
11. **Review and approval** - dashboard, quality gate, approval audit trail, safe script/scene editing, and downstream invalidation.
12. **Publishing** - explicit queue, hash enforcement, local export by default, and an optional official YouTube Data API adapter.

See [`docs/`](docs/) for the per-phase design notes.

## Approval and publishing safety

- Video renders start as `draft` and are never published by the rendering agent.
- The review API creates a `ContentApproval` record and runs deterministic checks before approval is allowed: the file exists and is large enough, subtitles contain timed cues, the timeline manifest is valid, duration and resolution are sane, and the source script passed its guardrails.
- Approval stores the SHA-256 hash of the exact MP4 that was reviewed.
- Queuing a publishing action requires `approval.status == "approved"`, `render.status == "approved"`, and an unchanged hash.
- The publishing agent re-checks both the approval hash and the hash pinned when the record was queued. If either changed, the record is blocked rather than uploaded.
- Approval is locked once publishing activity exists, so a queued or published artifact cannot be re-approved after modification.
- Corroboration is counted per publisher, not per feed. Trust-policy sources that share configured domains are grouped, and unconfigured feeds collapse to their registrable domain, so BBC World and BBC Technology count once even across `bbc.com` and `bbc.co.uk`.
- Publishing status (`queued`, `publishing`, `exported`, `published`, `failed`, `blocked`) is separate from editorial approval status.
- Every review decision and invalidation is recorded in `ApprovalEvent`, which is not deleted when media is regenerated.

## Quick start

Prerequisites:

- Python 3.11+
- FFmpeg on `PATH`, or a project-local binary configured with `NEWS_VIDEO_FFMPEG_EXECUTABLE` and `NEWS_TTS_FFMPEG_EXECUTABLE`
- Piper executable and voice model for narration
- Internet access on first use to download the local embedding model

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

The dashboard is an editorial operations console with six workspaces: a command center (next action, active productions, metrics, pipeline health, attention items, service readiness, recent runs), **Production** (one-click video generation), the review queue (source/evidence provenance, script and scene editors, quality checks, approval controls), agent runs, publishing records, and the article library. Stage actions are asynchronous: the UI triggers a stage, polls its agent run, and refreshes counts when it completes.

The interface uses one low-saturation charcoal palette with a restrained slate-blue accent. Color, type, spacing, radii, shadows, controls, and motion are centralized as CSS custom properties in `frontend/styles.css`; only a dark theme is implemented. Status is always paired with text, and the UI never implies that a render publishes itself.

### One-click production

Open **Production**, choose a story whose script is a `draft`, then press **Generate complete news video**. The preflight panel controls:

- editing style (18 presets), aspect ratio (`16:9`, `9:16`, or both), captions, and asset strategy;
- voice, destination, optional licensed music file, and whether to re-narrate;
- the configured defaults, remembered in `data/production_defaults.json`.

The job runs `prepare -> scenes -> assets -> narrate -> render -> verify`, streams stage logs, and keeps progress in the database across restarts. Failed stages can be retried from the dashboard or `POST /api/v1/production/jobs/{id}/retry`. Re-narrating a script invalidates its draft render and both aspect variants; an approved or published render is never invalidated implicitly. Production ends at `awaiting_approval`; publishing remains a separate human decision.

### Project-local media tools (Windows)

This workspace supports an ignored local toolchain so the app does not require system-wide installs:

```text
.tools/ffmpeg/bin/ffmpeg.exe
.tools/piper/piper/piper.exe
.tools/node/node.exe
models/piper/en_US-lessac-medium.onnx
```

`.tools/`, `models/*`, and `.env` are ignored by Git. Point the app at them in `.env`:

```dotenv
NEWS_TTS_PIPER_EXECUTABLE=.tools/piper/piper/piper.exe
NEWS_TTS_FFMPEG_EXECUTABLE=.tools/ffmpeg/bin/ffmpeg.exe
NEWS_VIDEO_FFMPEG_EXECUTABLE=.tools/ffmpeg/bin/ffmpeg.exe
```

See [`docs/local-tools.md`](docs/local-tools.md) for download and verification commands. `.env.example` remains the portable default for machines where the binaries are on `PATH`.

### Typical editorial run

1. Use **Advance pipeline** in the Overview next-action band to run deduplication through video rendering in order, or trigger individual stages. Collection and publishing stay explicit: run **Collect news** to fetch feeds and **Process queue** after approving drafts.
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
| Workflow triggers | `POST /api/v1/workflow/runs` with `{"stage": "<key>"}` (async, 202) plus the synchronous per-agent `POST /api/v1/{collection,deduplication,credibility,research,script-writer,scene-planner,tts,video-generator}/runs` compatibility endpoints |
| Production | `GET /api/v1/production/config`, `/production/defaults`, `/production/stories`, `/production/jobs`, `POST /production/jobs`, `POST /production/jobs/{id}/start`, `/production/jobs/{id}/retry`, `GET /production/jobs/{id}`, `/production/jobs/{id}/variants/{aspect}/file`, `/production/jobs/{id}/frames/{aspect}/{name}` |
| Articles and provenance | `GET /api/v1/articles`, `/articles/{id}/credibility`, `/articles/{id}/research`, `/articles/{id}/script` |
| Review | `GET /api/v1/reviews`, `/reviews/analytics`, `/reviews/{video_render_id}`, `POST /reviews/{video_render_id}/decision`, `PATCH /reviews/scripts/{id}`, `PATCH /reviews/scenes/{id}`, `GET /reviews/videos/{id}/file` |
| Publishing | `POST /api/v1/publishing/queue`, `/publishing/runs`, `GET /publishing/records`, `/publishing/analytics`, `GET /publishing/records/{id}/file` (local export), `POST /publishing/records/{id}/retry` |
| Operations | `GET /api/v1/agent-runs`, `/approval-events`, `/system/status`, `/system/overview` |
| Dashboard | `GET /dashboard` and its static assets |

`/system/status` reports scheduler state, journal counts, and capability readiness for video FFmpeg, TTS FFmpeg, Piper, local embeddings, local publishing, and YouTube credentials. `/system/overview` is the command-center feed: 24-hour collection, pipeline counts and latest-run status, current attention items, recent runs, service health, and last sync.

Concurrent triggers for the same agent are rejected with `409`, and a second manual trigger cannot overlap a scheduler run. Runs left `running` by an unclean shutdown are marked failed during startup reconciliation.

## Configuration

Configuration uses the `NEWS_` environment prefix and is documented in [`.env.example`](.env.example). The most important settings are:

- `NEWS_DATABASE_URL` - defaults to SQLite; any async SQLAlchemy URL is supported.
- `NEWS_SOURCES_FILE` and `NEWS_TRUST_POLICY_FILE` - editorial source and trust configuration.
- `NEWS_*_ENABLED` and `NEWS_*_INTERVAL_MINUTES` - scheduler control per agent.
- `NEWS_APPROVAL_MIN_VIDEO_BYTES` - minimum artifact size accepted by the quality gate.
- `NEWS_USER_AGENT` - HTTP identity used for feed and article requests. The default is `NewsAutomationPlatform/0.1`; add a real deployment name and contact address before production use.
- `NEWS_ASSET_USER_AGENT` - identity used for media retrieval. Wikimedia Commons rejects user agents without a contact URL, so the default includes `+https://localhost/`; set a real deployment URL before production use.
- `NEWS_ASSET_PROVIDER_PRIORITY`, `NEWS_ASSET_STRATEGY_DEFAULT`, `NEWS_ASSET_MIN_WIDTH`, `NEWS_ASSET_MIN_HEIGHT`, `NEWS_ASSET_MAX_BYTES` - visual retrieval policy. Wikimedia and NASA need no keys; `NEWS_PEXELS_API_KEY` and `NEWS_PIXABAY_API_KEY` enable the optional providers.
- `NEWS_VIDEO_ASPECTS`, `NEWS_VIDEO_LANDSCAPE_WIDTH/HEIGHT`, `NEWS_VIDEO_PORTRAIT_WIDTH/HEIGHT` - output compositions. Both aspects are rendered from one timeline; nothing is stretched.
- `NEWS_AUDIO_MASTER_ENABLED`, `NEWS_AUDIO_TARGET_LUFS`, `NEWS_AUDIO_TRUE_PEAK_DB`, `NEWS_AUDIO_MUSIC_GAIN_DB` - mastering and optional music mixing.
- `NEWS_PRODUCTION_DEFAULTS_FILE` - remembered preflight choices for the production workspace.
- `NEWS_PUBLISHING_DEFAULT_PLATFORM`, `NEWS_PUBLISHING_OUTPUT_DIR`, and the YouTube variables above.

## Quality checks

```powershell
$env:TEMP='C:\Users\Shiva\AppData\Local\Temp\opencode'
$env:TMP=$env:TEMP
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.\.tools\node\node.exe --check frontend/app.js
```

The temporary-directory override is only needed on Windows installations where the default pytest temp directory has restrictive ACLs. The Node check is optional and only validates the dashboard JavaScript.

The latest live one-click production run (job `805dfcc3-e2d0-44df-ad08-b6a3e5a3a4be`, VOX style, both aspect ratios, burned-in captions) completed all six stages and stopped at `awaiting_approval`: 38.623 seconds, four licensed Wikimedia Commons photographs and two documented designed-graphic fallbacks, and two verified H.264/AAC/`mov_text` variants at 1920×1080 and 1080×1920 (5.9 MB and 5.6 MB). Both variants passed `file_exists`, `frame_size`, `audio_stream`, and `duration_sync`. The earlier committed six-publisher batch run used real sentence-transformer embeddings, Piper, and FFmpeg: 109 articles, 38 corroborated stories, 38 research briefs and scripts, one 40.66-second narration, and a 506,343-byte 1920x1080 render whose artifact was approved with SHA-256 `5d46fcafc29fc44c84f2f9f9b16f71092e6707376fd8d4bce01a1c5493ca94b9`; local export produced a matching metadata manifest. See [`docs/implementation-status.md`](docs/implementation-status.md).

## Current limitations

- The automated suite uses fake video and narration renderers for speed and determinism. Real FFmpeg/Piper execution is verified by the separate local smoke run, not by every `pytest` invocation.
- The first real deduplication run downloads the `all-MiniLM-L6-v2` model unless it is already cached; the model is not stored in the repository.
- The dashboard has no built-in login. It is designed for local or single-operator use behind a trusted network boundary.
- The schema is created with SQLAlchemy `create_all`; there is no migration framework yet.
- The optional YouTube adapter performs the upload request after recording an in-flight `publishing` attempt. If the process dies after the remote upload but before the result commit, reconcile the record manually before retrying to avoid a duplicate upload. A live YouTube upload has not been exercised.
- Music is never bundled. Supply a track you are licensed to distribute with `NEWS_AUDIO_MUSIC_DIR` or the music field in the production panel.
- Wikimedia Commons requires a compliant user agent with a contact URL and applies rate limits. NASA's image library is space-focused, so unrelated news scenes intentionally fall back to designed graphics rather than irrelevant imagery.
- Asset relevance depends on the story's own vocabulary; the pipeline re-derives search terms from the narration, but it does not perform semantic image search.
