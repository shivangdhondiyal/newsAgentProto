# Implementation status

This document records what has been verified in the repository, what is complete, and what remains environment-dependent. It is a snapshot, not a substitute for running the tests.

## Completed and verified

| Phase | Scope | Verification |
| --- | --- | --- |
| 1 | RSS collection, extraction, normalization, storage, audit runs | `tests/test_ingestion.py`, `tests/test_rss.py` |
| 2 | Local embedding deduplication with confirmed/review clusters | `tests/test_deduplication.py` |
| 3 | Credibility scoring, trust policy, evidence persistence, manual review, duplicate independence | `tests/test_credibility.py`, `tests/test_independence.py` |
| 4 | Research briefs, source retention, optional GDELT boundary | `tests/test_research.py` |
| 5 | Neutral script generation and guardrail flags | `tests/test_script_writer.py` |
| 6 | Scene planning and timing | `tests/test_scene_planner.py` |
| 7 | Chunked local TTS pipeline and timing metadata | `tests/test_tts.py` |
| 8 | Draft video rendering with subtitle/timeline artifacts | `tests/test_video_generator.py` |
| 9 | Review dashboard, quality gate, hash-pinned approval, audited editing, downstream invalidation | `tests/test_review_workflow.py`, `tests/test_dashboard.py`, routed flow in `tests/test_api_integration.py` |
| 10 | Approval-gated publishing, local export, optional YouTube upload, retries, analytics | `tests/test_publishing.py`, routed flow in `tests/test_api_integration.py` |

Latest verified local run (2026-10-27):

```text
pytest: 44 passed
ruff: All checks passed
node --check frontend/app.js: passed
Lighthouse (dashboard overview and review): accessibility 100, best practices 100, SEO 100
```

Run the same checks with:

```powershell
$env:TEMP='C:\Users\Shiva\AppData\Local\Temp\opencode'
$env:TMP=$env:TEMP
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
```

## Real-media and live-pipeline verification (2026-10-25)

A separate smoke run exercised the real local toolchain installed under `.tools/` and `models/piper/`:

```text
FFmpeg:                9.0.2 essentials build
Piper voice:           en_US-lessac-medium
Embedding model:       sentence-transformers/all-MiniLM-L6-v2 (384 dimensions)
Deduplication:         completed, real confirmed cluster created
Narration:             14.505 s, 639,716-byte WAV and 58,531-byte MP3
Video:                 180,232-byte MP4, 1920x1080, H.264 + AAC + mov_text
Approval:              passed quality gate, SHA-256 pinned
Publishing:            local export succeeded with metadata manifest
Capability status:     video_ffmpeg, tts_ffmpeg, piper_tts,
                       local_embeddings, local_publishing all true
Dashboard JavaScript:  node --check passed (Node 22.23.4)
```

A full live pipeline run then used the committed `config/sources.yml` and `config/trust.yml` with six publishers:

```text
RSS collection:        109 articles from 6 publishers, 0 errors, 0 warnings
Deduplication:         completed with real all-MiniLM-L6-v2 embeddings
Credibility:           109 assessments, 38 corroborated stories
Research:              38 briefs, 80 source records
Scripts:               38 scripts, 31 scene plans
Narration:             40.66 s, 1,793,136-byte WAV and 163,126-byte MP3
Video:                 506,343-byte MP4, 1920x1080, H.264 + AAC + mov_text
Quality gate:          6/6 checks passed
Approval:              approved, SHA-256 5d46fcafc29fc44c84f2f9f9b16f71092e6707376fd8d4bce01a1c5493ca94b9
Publishing:            local export succeeded with metadata manifest
```

The run confirmed publisher-level independence in practice: BBC World and BBC Technology were grouped as one publisher, and corroborated stories drew on up to four distinct publisher groups. It also verified the NPR fix: NPR article pages now return normally with the default `NewsAutomationPlatform/0.1` user agent, after the previous placeholder contact string caused read timeouts.

The `.env` file used by this workspace points at the project-local binaries. `.tools/`, `models/*`, and `.env` are ignored by Git; see [`local-tools.md`](local-tools.md) to reproduce the setup.

## Editorial operations dashboard rewrite (2026-10-27)

The dashboard was rebuilt as a command center over the existing API, without mocks or schema changes:

- **Stability:** a process-local per-agent coordinator rejects overlapping manual and scheduled runs (`409`), workflow triggers are asynchronous (`202` + poll), and startup reconciliation marks orphaned `running` agent runs as failed with a diagnostic message. SQLite now uses WAL, foreign keys, a busy timeout, and a 30-second connection timeout.
- **Operations feed:** `GET /api/v1/system/overview` returns real 24-hour collection counts, per-stage counts and latest-run status, attention items (only the latest failure per agent), recent runs, service readiness, journal counts, and last sync.
- **Workspaces:** hash-routed Overview, Review queue, Agent runs, Publishing, and Articles views share a token-based dark design system. Stage cards trigger their own stage, show running/success/error state, prevent duplicate submissions, and skip completed stages. **Advance pipeline** runs deduplication through video; collection and publishing remain explicit.
- **Review and publishing:** evidence/script/storyboard/quality tabs, guarded decisions, unsaved-change protection, local export downloads via `GET /api/v1/publishing/records/{id}/file`, and a single publishing record per draft.

Live verification on 2026-10-27 used the real local database and toolchain: deduplication completed via a stage card (14 created, 2 updated), **Advance pipeline** processed seven stages end-to-end, TTS produced five narrations, video produced three renders, one draft passed the 6/6 quality gate and was approved, its local export was queued and processed (`exported`, 1 attempt, 503,311-byte MP4), and the export file was served with `video/mp4`. Overview metrics, attention items, recent runs, review tabs, filters, and all five views were checked at 1000px and via same-origin probes at 360, 390, and 768px with no page overflow. No console errors or failed requests were observed.

## Environment-dependent or not verified end-to-end

- **Real YouTube upload:** the official OAuth refresh and resumable-upload sequence is tested against mocked HTTP responses only. No live credentials or live channel were used.
- **Windows pytest temp ACL:** the default user temp directory rejects pytest cache writes in this environment. Tests pass when `TEMP`/`TMP` point to the approved opencode temp directory. This is an environment issue, not an application failure.
- **Voice-model licensing:** the Lessac voice model is suitable for local verification, but its model-card license and the FFmpeg build license should be reviewed before redistribution or commercial publishing.

## Known limitations and follow-up work

- The local binaries and model weights are intentionally not committed. A fresh clone must install FFmpeg, Piper, a voice, and the embedding dependencies; `docs/local-tools.md` records the setup used for verification.
- No database migrations: schema creation uses SQLAlchemy `create_all`. Existing databases created before Phases 9–10 need the new tables created by startup or a real migration tool.
- The dashboard has no authentication. It is intended for localhost or a trusted single-operator network; put authentication and TLS in front of it before exposing it.
- The visual layer remains a neutral background. A licensed asset resolver is future work; the timeline already carries the search and animation guidance.
- YouTube uploads read the whole MP4 into memory and do not implement chunked resume after a mid-upload connection failure.
- A crash after a remote upload succeeds but before the result is committed can leave a record in `publishing`. Reconcile it manually before retrying; retrying a `publishing` record is intentionally not automatic.
- Duplicate clusters marked `confirmed` or left in `review` are treated as one report for corroboration. Only a human-rejected duplicate cluster can contribute an independent source.
- Corroboration is counted per publisher. Trust-policy sources sharing configured domains are grouped, and unconfigured feeds collapse to their registrable domain, so multiple feeds from one publisher cannot create false independence.
- Text generation defaults to deterministic extractive logic. Optional local Ollama adapters exist, but no cloud LLM is used.
