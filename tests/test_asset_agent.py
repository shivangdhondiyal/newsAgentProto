from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.agents.assets.agent import AssetAgent
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    Article,
    NewsScript,
    ResearchBrief,
    Scene,
    SceneAsset,
    ScenePlan,
    VisualAsset,
)
from backend.app.media.assets import (
    AssetCandidate,
    AssetRetrievalResult,
    AssetSelection,
)


class FakePipeline:
    """Return one already-downloaded selection without touching the network."""

    def __init__(self, image_path: Path) -> None:
        self.image_path = image_path
        self.briefs: list[str] = []

    async def retrieve(self, brief, *, strategy: str, aspect_ratio: str):
        self.briefs.append(brief.query)
        self.image_path.parent.mkdir(parents=True, exist_ok=True)
        self.image_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        candidate = AssetCandidate(
            provider="wikimedia",
            provider_asset_id="File:Example.jpg",
            title="Example",
            description="Example image",
            source_url="https://commons.example/File:Example.jpg",
            download_url="https://commons.example/example.jpg",
            creator="Tester",
            license_name="CC BY 4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            attribution="Tester, CC BY 4.0",
        )
        return AssetRetrievalResult(
            selection=AssetSelection(
                candidate=candidate,
                local_path=self.image_path,
                checksum="abc123",
                mime_type="image/jpeg",
                file_size=16,
                width=1280,
                height=720,
                focal_x=0.5,
                focal_y=0.5,
                rank_score=0.82,
            )
        )


async def _seed(tmp_path: Path, *, local_path: Path, status: str = "ready"):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        article = Article(
            source_id="bbc-world",
            source_name="BBC News",
            url="https://bbc.example/lula",
            canonical_url="https://bbc.example/lula",
            title="Lula re-elected in Brazil",
            normalized_text="Lula re-elected in Brazil as Bolsonaro rejects results.",
        )
        session.add(article)
        await session.flush()
        brief = ResearchBrief(
            article_id=article.id,
            status="completed",
            headline=article.title,
            key_facts=[],
            context=[],
            uncertainties=[],
            source_count=1,
            summarizer_name="test",
        )
        session.add(brief)
        await session.flush()
        script = NewsScript(
            article_id=article.id,
            research_brief_id=brief.id,
            status="draft",
            title=article.title,
            hook="Hook.",
            introduction="Introduction.",
            main_story=["Main story."],
            context=[],
            closing="Closing.",
            full_text="Full text.",
            estimated_duration_seconds=10,
            target_duration_seconds=60,
            writer_name="test",
            review_reasons=[],
        )
        session.add(script)
        await session.flush()
        plan = ScenePlan(
            script_id=script.id,
            status="draft",
            total_duration_seconds=10,
            planner_name="test",
        )
        session.add(plan)
        await session.flush()
        scene = Scene(
            scene_plan_id=plan.id,
            position=1,
            start_seconds=0,
            duration_seconds=5,
            narration="Lula re-elected in Brazil as Bolsonaro rejects results.",
            subtitle="Lula re-elected in Brazil as Bolsonaro rejects results.",
            keywords=["lula", "brazil"],
            scene_description="Supporters in Brasilia.",
            stock_footage_query="lula brazil",
            image_search_query="lula brazil",
            animation_suggestion="slow push",
            transition="crossfade",
        )
        session.add(scene)
        await session.flush()
        asset = VisualAsset(
            provider="wikimedia",
            provider_asset_id="File:Example.jpg",
            query="lula brazil",
            title="Example",
            source_url="https://commons.example/File:Example.jpg",
            download_url="https://commons.example/example.jpg",
            license_name="CC BY 4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            attribution="Tester, CC BY 4.0",
            media_type="image",
            local_path=str(local_path),
            mime_type="image/jpeg",
            file_size=16,
            width=1280,
            height=720,
            checksum="abc123",
            status="valid",
        )
        session.add(asset)
        await session.flush()
        session.add(
            SceneAsset(
                scene_id=scene.id,
                asset_id=asset.id if status == "ready" else None,
                role="primary",
                relevance_score=0.82 if status == "ready" else 0.0,
                match_reason="wikimedia match" if status == "ready" else "fallback",
                status=status,
            )
        )
        await session.commit()
    return engine, sessions


@pytest.mark.asyncio
async def test_missing_cached_file_is_retrieved_again(tmp_path: Path) -> None:
    missing = tmp_path / "cache" / "missing.jpg"
    engine, sessions = await _seed(tmp_path, local_path=missing)
    pipeline = FakePipeline(tmp_path / "cache" / "fresh.jpg")
    agent = AssetAgent(
        Settings(asset_cache_dir=tmp_path / "cache"),
        sessions,
        pipeline=pipeline,
    )

    result = await agent.execute(script_id=None, strategy="hybrid", aspect_ratio="16:9")

    assert result.items_seen == 1
    assert result.items_created == 0
    assert any("cached asset file was missing" in warning for warning in result.warnings)
    async with sessions() as session:
        rows = list((await session.scalars(select(SceneAsset))).all())
        assert len(rows) == 1
        assert rows[0].status == "ready"
        assert rows[0].relevance_score == pytest.approx(0.82)
        stored = await session.scalar(select(VisualAsset))
        assert stored is not None
        assert Path(stored.local_path) == tmp_path / "cache" / "fresh.jpg"
    await engine.dispose()


@pytest.mark.asyncio
async def test_present_cached_file_is_not_retrieved_again(tmp_path: Path) -> None:
    present = tmp_path / "cache" / "present.jpg"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    engine, sessions = await _seed(tmp_path, local_path=present)
    pipeline = FakePipeline(tmp_path / "cache" / "unused.jpg")
    agent = AssetAgent(
        Settings(asset_cache_dir=tmp_path / "cache"),
        sessions,
        pipeline=pipeline,
    )

    result = await agent.execute(strategy="hybrid", aspect_ratio="16:9")

    assert result.items_seen == 0
    assert pipeline.briefs == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_documented_fallback_is_left_alone(tmp_path: Path) -> None:
    engine, sessions = await _seed(tmp_path, local_path=tmp_path / "none.jpg", status="fallback")
    pipeline = FakePipeline(tmp_path / "cache" / "unused.jpg")
    agent = AssetAgent(
        Settings(asset_cache_dir=tmp_path / "cache"),
        sessions,
        pipeline=pipeline,
    )

    result = await agent.execute(strategy="hybrid", aspect_ratio="16:9")

    assert result.items_seen == 0
    assert pipeline.briefs == []
    await engine.dispose()
