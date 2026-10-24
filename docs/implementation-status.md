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

Latest verified local run:

```text
pytest: 30 passed
ruff: All checks passed
```

Run the same checks with:

```powershell
$env:TEMP='C:\Users\Shiva\AppData\Local\Temp\opencode'
$env:TMP=$env:TEMP
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
```

## Environment-dependent or not verified end-to-end

- **Real FFmpeg rendering:** FFmpeg is not on `PATH` in the development environment. Video tests use a fake renderer. The `/system/status` capability report exposes `video_ffmpeg: false` until FFmpeg is installed.
- **Real Piper narration:** the Piper executable and voice model are not present. TTS tests use a fake synthesizer. `/system/status` exposes `piper_tts` readiness.
- **Real semantic embeddings:** `sentence-transformers` is declared in `pyproject.toml` but is not installed in the current `.venv`, so no real model inference was executed. Deduplication tests use deterministic test vectors/providers.
- **Real YouTube upload:** the official OAuth refresh and resumable-upload sequence is tested against mocked HTTP responses only. No live credentials were used.
- **JavaScript syntax/runtime check:** Node.js was not available in the environment. The dashboard is covered by asset-serving tests and manual review, but not by a JS parser or browser test.
- **Windows pytest temp ACL:** the default user temp directory rejects pytest cache writes in this environment. Tests pass when `TEMP`/`TMP` point to the approved opencode temp directory. This is an environment issue, not an application failure.

## Known limitations and follow-up work

- No database migrations: schema creation uses SQLAlchemy `create_all`. Existing databases created before Phases 9–10 need the new tables created by startup or a real migration tool.
- The dashboard has no authentication. It is intended for localhost or a trusted single-operator network; put authentication and TLS in front of it before exposing it.
- The visual layer remains a neutral background. A licensed asset resolver is future work; the timeline already carries the search and animation guidance.
- YouTube uploads read the whole MP4 into memory and do not implement chunked resume after a mid-upload connection failure.
- A crash after a remote upload succeeds but before the result is committed can leave a record in `publishing`. Reconcile it manually before retrying; retrying a `publishing` record is intentionally not automatic.
- Duplicate clusters marked `confirmed` or left in `review` are treated as one report for corroboration. Only a human-rejected duplicate cluster can contribute an independent source.
- Text generation defaults to deterministic extractive logic. Optional local Ollama adapters exist, but no cloud LLM is used.
