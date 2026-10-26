from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agents.errors import AgentRunFailed
from backend.app.agents.tts.audio import merge_and_normalize_wav, wave_metadata
from backend.app.agents.tts.contracts import SpeechSynthesizer
from backend.app.agents.tts.providers import PiperSpeechSynthesizer
from backend.app.core.config import Settings
from backend.app.db.models import AgentRun, NarrationAudio, NarrationChunk, NewsScript, ScenePlan
from backend.app.schemas.tts import TTSRunRead


class TTSAgent:
    """Chunks a script, creates local narration, and retains synchronization metadata."""

    name = "tts"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        synthesizer: SpeechSynthesizer | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._synthesizer = synthesizer or self._create_synthesizer(settings)

    async def execute(self) -> TTSRunRead:
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        run = AgentRun(agent_name=self.name, status="running", started_at=started_at)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            try:
                candidates = await self._load_eligible_scripts(session)
                run.items_seen = len(candidates)
                for script, scene_plan in candidates:
                    await self._create_narration(session, script, scene_plan)
                    run.items_created += 1
                run.status = "completed"
            except Exception as exc:
                run.status = "failed"
                run.errors = [*run.errors, str(exc)]
            finally:
                run.finished_at = datetime.now(UTC)
                run.duration_ms = round((perf_counter() - started_clock) * 1000, 2)
                await session.commit()
                await session.refresh(run)

        if run.status == "failed":
            raise AgentRunFailed(self.name, run.id, run.errors[-1], run.errors)
        return TTSRunRead.model_validate(run)

    async def _load_eligible_scripts(
        self, session: AsyncSession
    ) -> list[tuple[NewsScript, ScenePlan]]:
        statement = (
            select(NewsScript, ScenePlan)
            .join(ScenePlan, ScenePlan.script_id == NewsScript.id)
            .outerjoin(NarrationAudio, NarrationAudio.script_id == NewsScript.id)
            .where(
                NewsScript.status == "draft",
                ScenePlan.status == "draft",
                NarrationAudio.id.is_(None),
            )
            .order_by(NewsScript.created_at.asc())
            .limit(self._settings.tts_batch_size)
        )
        return list((await session.execute(statement)).tuples().all())

    async def _create_narration(
        self, session: AsyncSession, script: NewsScript, scene_plan: ScenePlan
    ) -> None:
        chunks = self._chunk_text(script.full_text)
        output_dir = self._settings.tts_output_dir / script.id
        chunk_paths: list[Path] = []
        chunk_metadata = []
        for position, text in enumerate(chunks, start=1):
            path = output_dir / f"chunk-{position:03d}.wav"
            await self._synthesizer.synthesize(text, path)
            chunk_paths.append(path)
            chunk_metadata.append(wave_metadata(path))
        wav_path = output_dir / "narration.wav"
        merged = merge_and_normalize_wav(chunk_paths, wav_path)
        mp3_path = await self._export_mp3(wav_path) if self._settings.tts_generate_mp3 else None
        narration = NarrationAudio(
            script_id=script.id,
            scene_plan_id=scene_plan.id,
            status="completed",
            provider_name=self._synthesizer.name,
            voice_name=self._synthesizer.voice_name,
            wav_path=str(wav_path),
            mp3_path=str(mp3_path) if mp3_path else None,
            duration_seconds=round(merged.duration_seconds, 3),
            sample_rate=merged.sample_rate,
        )
        session.add(narration)
        await session.flush()
        start_seconds = 0.0
        for position, (text, path, metadata) in enumerate(
            zip(chunks, chunk_paths, chunk_metadata, strict=True), start=1
        ):
            duration = round(metadata.duration_seconds, 3)
            session.add(
                NarrationChunk(
                    narration_audio_id=narration.id,
                    position=position,
                    text=text,
                    wav_path=str(path),
                    start_seconds=round(start_seconds, 3),
                    duration_seconds=duration,
                )
            )
            start_seconds += duration

    def _chunk_text(self, text: str) -> list[str]:
        sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if len(sentence) > self._settings.tts_chunk_max_characters:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(self._split_long_text(sentence))
            elif (
                current
                and len(current) + len(sentence) + 1 > self._settings.tts_chunk_max_characters
            ):
                chunks.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            chunks.append(current)
        if not chunks:
            raise ValueError("Script contains no narration text")
        return chunks

    def _split_long_text(self, text: str) -> list[str]:
        words = text.split()
        chunks: list[str] = []
        current = ""
        for word in words:
            if current and len(current) + len(word) + 1 > self._settings.tts_chunk_max_characters:
                chunks.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            chunks.append(current)
        return chunks

    async def _export_mp3(self, wav_path: Path) -> Path:
        mp3_path = wav_path.with_suffix(".mp3")
        process = await asyncio.create_subprocess_exec(
            self._settings.tts_ffmpeg_executable,
            "-y",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            str(mp3_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not mp3_path.is_file():
            raise RuntimeError(
                f"FFmpeg MP3 export failed: {stderr.decode('utf-8', errors='replace')}"
            )
        return mp3_path

    @staticmethod
    def _create_synthesizer(settings: Settings) -> SpeechSynthesizer:
        if settings.tts_provider == "piper":
            return PiperSpeechSynthesizer(
                settings.tts_piper_executable, settings.tts_piper_voice_model
            )
        raise ValueError("Unsupported TTS provider; use 'piper'")
