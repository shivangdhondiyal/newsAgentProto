from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageSpec:
    """Static description of one editorial pipeline stage.

    The dashboard and the dispatcher share this registry so labels, descriptions,
    and agent names cannot drift apart.
    """

    key: str
    label: str
    description: str
    agent_name: str


STAGE_SPECS: tuple[StageSpec, ...] = (
    StageSpec(
        key="collection",
        label="Collect news",
        description="Fetch configured RSS feeds and normalize new articles.",
        agent_name="news_collector",
    ),
    StageSpec(
        key="deduplication",
        label="Deduplicate",
        description="Cluster syndicated and near-identical reports.",
        agent_name="deduplication",
    ),
    StageSpec(
        key="credibility",
        label="Check credibility",
        description="Score corroboration and flag unsupported claims.",
        agent_name="credibility",
    ),
    StageSpec(
        key="research",
        label="Build research",
        description="Create an evidence brief with source context.",
        agent_name="research",
    ),
    StageSpec(
        key="script",
        label="Write scripts",
        description="Draft neutral narration from the verified brief.",
        agent_name="script_writer",
    ),
    StageSpec(
        key="scenes",
        label="Plan scenes",
        description="Break the script into timed visual scenes.",
        agent_name="scene_planner",
    ),
    StageSpec(
        key="assets",
        label="Source visuals",
        description="Retrieve licensed, validated imagery or record a designed fallback.",
        agent_name="asset_retriever",
    ),
    StageSpec(
        key="tts",
        label="Narrate",
        description="Synthesize mastered narration with sentence and paragraph pacing.",
        agent_name="tts",
    ),
    StageSpec(
        key="video",
        label="Render video",
        description="Compose scene graphics, motion, captions, and audio into MP4 variants.",
        agent_name="video_generator",
    ),
    StageSpec(
        key="publishing",
        label="Process publishing",
        description="Send approved renders to configured platforms.",
        agent_name="publishing",
    ),
)

STAGE_BY_KEY = {spec.key: spec for spec in STAGE_SPECS}
STAGE_BY_AGENT = {spec.agent_name: spec for spec in STAGE_SPECS}
