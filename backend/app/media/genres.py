from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from backend.app.media.presets import EditingPreset, get_preset
from backend.app.media.script_text import strip_meta_sentences

AUTO_GENRE = "auto"

# How a genre's default presentation may be overridden without changing the
# story's subject. These are deliberately separate from the genre itself.
MOTION_INTENSITY_LABELS = {
    "restrained": 0.55,
    "balanced": 1.0,
    "expressive": 1.35,
}


@dataclass(frozen=True)
class GenreProfile:
    """A shared, configurable production profile for one news genre.

    Every downstream stage reads this object instead of scattering
    ``if genre == ...`` branches through the pipeline. A genre describes what
    the story *is*; the editing preset and the user overrides describe how it is
    presented.
    """

    key: str
    name: str
    description: str
    default_approach: str
    default_style: str
    script_tone: str
    script_structure: str
    script_pacing: str
    narration_rate: float = 1.04
    narration_expressiveness: float = 0.667
    sentence_pause_ms: int = 180
    paragraph_pause_ms: int = 420
    asset_priority: tuple[str, ...] = ("photograph", "broll", "document")
    footage_ratio: float = 0.35
    shot_mix: tuple[str, ...] = ("wide", "medium", "close")
    motion: str = "slow_zoom"
    motion_intensity: float = 0.35
    transition: str = "fade"
    graphic_templates: tuple[str, ...] = ("lower_third", "stat", "source")
    graphic_density: float = 0.35
    caption_preset: str = "clean_editorial"
    target_lufs: float = -16.0
    music_enabled: bool = True
    music_intensity: float = 0.35
    music_moods: tuple[str, ...] = ("calm", "neutral", "editorial")
    ducking: bool = True
    sfx_enabled: bool = False
    sfx_intensity: float = 0.15
    research_depth: str = "standard"
    source_requirement: int = 2
    sensitivity: str = "standard"
    lower_third: bool = True
    ticker: bool = False
    editorial_notes: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "default_approach": self.default_approach,
            "default_style": self.default_style,
            "script": {
                "tone": self.script_tone,
                "structure": self.script_structure,
                "pacing": self.script_pacing,
            },
            "narration": {
                "rate": self.narration_rate,
                "expressiveness": self.narration_expressiveness,
                "sentence_pause_ms": self.sentence_pause_ms,
                "paragraph_pause_ms": self.paragraph_pause_ms,
            },
            "visuals": {
                "asset_priority": list(self.asset_priority),
                "footage_ratio": self.footage_ratio,
                "shot_mix": list(self.shot_mix),
                "motion": self.motion,
                "motion_intensity": self.motion_intensity,
                "transition": self.transition,
                "graphic_templates": list(self.graphic_templates),
                "graphic_density": self.graphic_density,
            },
            "captions_preset": self.caption_preset,
            "audio": {
                "target_lufs": self.target_lufs,
                "music_enabled": self.music_enabled,
                "music_intensity": self.music_intensity,
                "music_moods": list(self.music_moods),
                "ducking": self.ducking,
                "sfx_enabled": self.sfx_enabled,
                "sfx_intensity": self.sfx_intensity,
            },
            "research_depth": self.research_depth,
            "source_requirement": self.source_requirement,
            "sensitivity": self.sensitivity,
            "lower_third": self.lower_third,
            "ticker": self.ticker,
            "editorial_notes": list(self.editorial_notes),
        }


def _genre(
    key: str,
    name: str,
    description: str,
    default_approach: str,
    default_style: str,
    **kwargs: Any,
) -> GenreProfile:
    return GenreProfile(
        key=key,
        name=name,
        description=description,
        default_approach=default_approach,
        default_style=default_style,
        script_tone=kwargs.pop("script_tone", "neutral, factual"),
        script_structure=kwargs.pop("script_structure", "hook → development → resolution"),
        script_pacing=kwargs.pop("script_pacing", "measured"),
        **kwargs,
    )


GENRES: dict[str, GenreProfile] = {
    "breaking_news": _genre(
        "breaking_news",
        "Breaking News",
        "Fast, verified updates with concise narration and unmistakable context.",
        "Short sentences, hard cuts, timestamped update bands, minimal decoration.",
        "breaking",
        script_tone="direct, urgent but restrained",
        script_structure="verified update → what is known → what is not → what next",
        script_pacing="quick",
        narration_rate=1.08,
        sentence_pause_ms=130,
        paragraph_pause_ms=300,
        asset_priority=("broll", "photograph", "map", "document"),
        footage_ratio=0.55,
        shot_mix=("wide", "medium", "close"),
        motion="static",
        motion_intensity=0.12,
        transition="cut",
        graphic_templates=("lower_third", "timeline", "map", "ticker", "source"),
        graphic_density=0.45,
        caption_preset="professional_news",
        target_lufs=-16.0,
        music_enabled=False,
        music_intensity=0.0,
        sfx_enabled=False,
        sfx_intensity=0.0,
        research_depth="fast verification",
        source_requirement=2,
        ticker=True,
        editorial_notes=(
            "Do not label a story breaking without a verified, recent update.",
            "Keep the timestamp and location visible.",
        ),
    ),
    "general_news": _genre(
        "general_news",
        "General News",
        "Balanced daily reporting with clean B-roll, lower thirds, and a composed pace.",
        "Straightforward narration, relevant footage, restrained editorial graphics.",
        "broadcast",
        script_pacing="measured",
        asset_priority=("photograph", "broll", "document", "map"),
        footage_ratio=0.45,
        graphic_templates=("lower_third", "stat", "map", "source"),
        graphic_density=0.35,
        caption_preset="clean_editorial",
        editorial_notes=("Keep attribution on screen whenever a claim is contested.",),
    ),
    "politics_government": _genre(
        "politics_government",
        "Politics & Government",
        "Institutional context, timelines, policy comparisons, and official-source references.",
        "Explanatory narration with maps, timelines, and clearly attributed statements.",
        "broadcast",
        script_tone="precise, non-partisan",
        script_structure="decision → institutions → comparison → consequence",
        script_pacing="measured",
        asset_priority=("photograph", "document", "map", "broll"),
        footage_ratio=0.3,
        shot_mix=("wide-map", "medium", "close-label", "document"),
        motion="slow_pan",
        motion_intensity=0.25,
        transition="fade",
        graphic_templates=("lower_third", "timeline", "map", "comparison", "source"),
        graphic_density=0.6,
        caption_preset="clean_editorial",
        music_intensity=0.25,
        music_moods=("neutral", "editorial", "calm"),
        research_depth="institutional context",
        source_requirement=3,
        editorial_notes=(
            "Distinguish a proposal from an enacted policy.",
            "Attribute every quotation to a named speaker and role.",
        ),
    ),
    "business_economy": _genre(
        "business_economy",
        "Business & Economy",
        "Charts, financial figures, company context, and clear comparisons.",
        "Data-led narration with precise labels and controlled motion graphics.",
        "data",
        script_tone="precise, explanatory",
        script_structure="finding → figures → comparison → implication",
        script_pacing="measured",
        asset_priority=("chart", "document", "photograph", "broll"),
        footage_ratio=0.25,
        shot_mix=("wide", "medium-chart", "close-stat", "evidence"),
        motion="static",
        motion_intensity=0.18,
        transition="cut",
        graphic_templates=("chart", "stat", "comparison", "lower_third", "source"),
        graphic_density=0.7,
        caption_preset="clean_editorial",
        target_lufs=-16.0,
        music_intensity=0.22,
        music_moods=("neutral", "calm", "corporate"),
        research_depth="financial context",
        source_requirement=3,
        editorial_notes=(
            "Use actual reported figures; never infer a missing number.",
            "Keep units, dates, and comparison baselines on screen.",
        ),
    ),
    "technology_ai": _genre(
        "technology_ai",
        "Technology & AI",
        "Product demonstrations, interface footage, technical diagrams, and animated explanations.",
        "Explanatory sequences with progressive diagrams and restrained interface callouts.",
        "diagram",
        script_tone="explanatory, evidence-led",
        script_structure="capability → mechanism → evidence → limitation",
        script_pacing="measured",
        asset_priority=("diagram", "broll", "photograph", "document"),
        footage_ratio=0.4,
        shot_mix=("wide-diagram", "medium-graphic", "close-label"),
        motion="parallax",
        motion_intensity=0.35,
        transition="fade",
        graphic_templates=("diagram", "callout", "stat", "lower_third", "source"),
        graphic_density=0.65,
        caption_preset="clean_editorial",
        music_intensity=0.3,
        music_moods=("ambient", "neutral", "modern"),
        research_depth="technical context",
        source_requirement=3,
        editorial_notes=(
            "Separate a demonstration from an independent evaluation.",
            "Do not animate a capability the source does not demonstrate.",
        ),
    ),
    "science_research": _genre(
        "science_research",
        "Science & Research",
        "Explanatory diagrams, experiments, data visualisations, and careful uncertainty.",
        "Method-first narration with diagrams and explicit distinctions between findings.",
        "diagram",
        script_tone="careful, explanatory",
        script_structure="finding → method → evidence → caveat",
        script_pacing="measured",
        asset_priority=("diagram", "document", "photograph", "chart"),
        footage_ratio=0.2,
        shot_mix=("wide-diagram", "medium-chart", "close-label", "evidence"),
        motion="slow_pan",
        motion_intensity=0.22,
        transition="fade",
        graphic_templates=("diagram", "chart", "timeline", "callout", "source"),
        graphic_density=0.7,
        caption_preset="clean_editorial",
        music_intensity=0.2,
        music_moods=("ambient", "calm", "neutral"),
        research_depth="peer-reviewed context",
        source_requirement=3,
        editorial_notes=(
            "State whether a result is preliminary, peer reviewed, or replicated.",
            "Never present a correlation as a proven cause.",
        ),
    ),
    "world_geopolitics": _genre(
        "world_geopolitics",
        "World & Geopolitics",
        "Location maps, geographic context, timelines, and clearly attributed information.",
        "Location-first narration with illustrative maps and careful uncertainty labels.",
        "map_geopolitical",
        script_tone="precise, contextual",
        script_structure="location → development → interests → uncertainty",
        script_pacing="measured",
        asset_priority=("map", "broll", "photograph", "document"),
        footage_ratio=0.4,
        shot_mix=("wide-map", "medium-map", "close-label", "evidence"),
        motion="map_drift",
        motion_intensity=0.4,
        transition="fade",
        graphic_templates=("map", "timeline", "callout", "lower_third", "source"),
        graphic_density=0.65,
        caption_preset="clean_editorial",
        music_intensity=0.25,
        music_moods=("tense", "neutral", "editorial"),
        research_depth="geographic and diplomatic context",
        source_requirement=3,
        editorial_notes=(
            "Mark disputed borders and unverified locations explicitly.",
            "Do not invent routes or coordinates.",
        ),
    ),
    "crime_investigation": _genre(
        "crime_investigation",
        "Crime & Investigation",
        "Evidence-focused presentation, verified timelines, and restrained visuals.",
        "Careful narration with documents, timelines, and explicit allegation labels.",
        "documentary",
        script_tone="careful, evidence-led",
        script_structure="allegation → evidence → timeline → open questions",
        script_pacing="lingering",
        narration_rate=0.98,
        asset_priority=("document", "photograph", "archival", "map"),
        footage_ratio=0.2,
        shot_mix=("wide", "medium", "document", "close"),
        motion="slow_pan",
        motion_intensity=0.18,
        transition="dissolve",
        graphic_templates=("timeline", "callout", "quote", "lower_third", "source"),
        graphic_density=0.55,
        caption_preset="clean_editorial",
        target_lufs=-16.0,
        music_enabled=False,
        music_intensity=0.0,
        sfx_enabled=False,
        sfx_intensity=0.0,
        sensitivity="sensitive",
        research_depth="documentary verification",
        source_requirement=3,
        editorial_notes=(
            "Say 'alleged' until a court or primary record confirms the claim.",
            "Do not use dramatic stingers or accusatory visual framing.",
        ),
    ),
    "disaster_humanitarian": _genre(
        "disaster_humanitarian",
        "Disaster & Humanitarian",
        "Factual reporting, verified event footage, geographic context, and respectful sound.",
        "Calm narration, clear maps, restrained motion, and no sensational sound design.",
        "photojournalism",
        script_tone="calm, respectful, factual",
        script_structure="what happened → who is affected → verified response → how to help",
        script_pacing="measured",
        narration_rate=0.96,
        sentence_pause_ms=240,
        paragraph_pause_ms=520,
        asset_priority=("broll", "photograph", "map", "document"),
        footage_ratio=0.5,
        shot_mix=("wide", "medium", "close"),
        motion="ken_burns",
        motion_intensity=0.18,
        transition="fade",
        graphic_templates=("map", "lower_third", "stat", "source"),
        graphic_density=0.35,
        caption_preset="clean_editorial",
        target_lufs=-16.0,
        music_enabled=False,
        music_intensity=0.0,
        sfx_enabled=False,
        sfx_intensity=0.0,
        sensitivity="high",
        research_depth="humanitarian verification",
        source_requirement=3,
        editorial_notes=(
            "Avoid graphic imagery, dramatic impacts, and disaster sound effects.",
            "Do not identify victims without confirmation and consent.",
        ),
    ),
    "sports_news": _genre(
        "sports_news",
        "Sports News",
        "Match footage where licensed, player and team visuals, statistics, and timelines.",
        "Energetic but readable narration with score graphics and clear identifiers.",
        "broadcast",
        script_tone="energetic, factual",
        script_structure="result → key moment → context → next fixture",
        script_pacing="quick",
        narration_rate=1.08,
        sentence_pause_ms=140,
        paragraph_pause_ms=320,
        asset_priority=("broll", "photograph", "stat", "document"),
        footage_ratio=0.6,
        shot_mix=("close", "medium", "wide"),
        motion="punch",
        motion_intensity=0.45,
        transition="cut",
        graphic_templates=("stat", "lower_third", "timeline", "comparison", "source"),
        graphic_density=0.6,
        caption_preset="kinetic",
        music_intensity=0.4,
        music_moods=("uplifting", "energetic", "neutral"),
        sfx_enabled=True,
        sfx_intensity=0.18,
        research_depth="results and records",
        source_requirement=2,
        editorial_notes=(
            "Only use match footage that is licensed for this distribution.",
            "Keep score, competition, and date visible with statistics.",
        ),
    ),
    "entertainment_celebrity": _genre(
        "entertainment_celebrity",
        "Entertainment & Celebrity",
        "Relevant event footage, interviews, entertainment graphics, and conversational pacing.",
        "Conversational narration with lower thirds, pull quotes, and varied pacing.",
        "magazine",
        script_tone="conversational, factual",
        script_structure="moment → context → reaction → what next",
        script_pacing="measured",
        narration_rate=1.02,
        asset_priority=("photograph", "broll", "document", "chart"),
        footage_ratio=0.5,
        shot_mix=("medium", "close", "wide"),
        motion="ken_burns",
        motion_intensity=0.32,
        transition="dissolve",
        graphic_templates=("lower_third", "quote", "callout", "source"),
        graphic_density=0.45,
        caption_preset="social_media",
        music_intensity=0.35,
        music_moods=("uplifting", "light", "modern"),
        sfx_enabled=True,
        sfx_intensity=0.12,
        research_depth="public-record context",
        source_requirement=2,
        editorial_notes=(
            "Do not repeat unverified tabloid claims as fact.",
            "Identify handlers, publicists, or representatives accurately.",
        ),
    ),
    "documentary_deep_dive": _genre(
        "documentary_deep_dive",
        "Documentary / Deep Dive",
        "Longer visual sequences, archival material, timelines, and detailed explanatory graphics.",
        "Contextual narration, longer shots, restrained motion, and subtle music.",
        "documentary",
        script_tone="reflective, evidence-led",
        script_structure="cold open → history → evidence → unresolved questions",
        script_pacing="lingering",
        narration_rate=0.96,
        sentence_pause_ms=260,
        paragraph_pause_ms=600,
        asset_priority=("archival", "document", "photograph", "broll"),
        footage_ratio=0.3,
        shot_mix=("wide", "medium", "document", "close"),
        motion="slow_pan",
        motion_intensity=0.2,
        transition="dissolve",
        graphic_templates=("timeline", "map", "quote", "lower_third", "source"),
        graphic_density=0.5,
        caption_preset="clean_editorial",
        music_intensity=0.18,
        music_moods=("ambient", "calm", "tense"),
        research_depth="long-form investigation",
        source_requirement=4,
        editorial_notes=(
            "Label archival footage with its original date and source.",
            "Do not imply that archival material depicts the present.",
        ),
    ),
    "explainer_educational": _genre(
        "explainer_educational",
        "Explainer / Educational",
        "Step-by-step explanations, animated diagrams, maps, and a clear information hierarchy.",
        "Progressive explanation with one idea per beat and consistent transitions.",
        "vox",
        script_tone="clear, explanatory",
        script_structure="question → mechanism → example → takeaway",
        script_pacing="measured",
        asset_priority=("diagram", "map", "chart", "photograph", "broll"),
        footage_ratio=0.2,
        shot_mix=("wide-map", "medium-graphic", "close-stat", "evidence"),
        motion="parallax",
        motion_intensity=0.35,
        transition="cut",
        graphic_templates=("diagram", "chart", "map", "callout", "stat", "source"),
        graphic_density=0.8,
        caption_preset="clean_editorial",
        music_intensity=0.22,
        music_moods=("ambient", "neutral", "light"),
        research_depth="explanatory context",
        source_requirement=3,
        editorial_notes=(
            "Reveal each step when the narration introduces it.",
            "Do not use an arrow to imply a causal link the sources do not support.",
        ),
    ),
    "data_journalism": _genre(
        "data_journalism",
        "Data-Driven Journalism",
        "Charts, evidence visualisation, comparisons, statistics, and source-aware explanations.",
        "Numbers first: annotated charts, consistent units, and explicit sources.",
        "data",
        script_tone="analytical, precise",
        script_structure="finding → method → evidence → limitation",
        script_pacing="measured",
        asset_priority=("chart", "document", "map", "photograph"),
        footage_ratio=0.1,
        shot_mix=("wide", "medium-chart", "close-stat", "evidence"),
        motion="static",
        motion_intensity=0.15,
        transition="cut",
        graphic_templates=("chart", "stat", "comparison", "timeline", "source"),
        graphic_density=0.85,
        caption_preset="clean_editorial",
        music_intensity=0.15,
        music_moods=("neutral", "calm"),
        research_depth="dataset and methodology",
        source_requirement=4,
        editorial_notes=(
            "Show the source and the time period for every dataset.",
            "Never truncate an axis in a way that exaggerates a difference.",
        ),
    ),
    "interview_quote": _genre(
        "interview_quote",
        "Interview / Quote-Led",
        "Interview footage, attributed quotations, speaker identification, and supporting visuals.",
        "Speaker-led narration with pull quotes, lower thirds, and evidence cutaways.",
        "interview",
        script_tone="attributed, measured",
        script_structure="speaker → statement → context → evidence",
        script_pacing="measured",
        narration_rate=0.98,
        asset_priority=("broll", "photograph", "document", "archival"),
        footage_ratio=0.55,
        shot_mix=("medium", "close", "wide", "document"),
        motion="slow_zoom",
        motion_intensity=0.2,
        transition="cut",
        graphic_templates=("quote", "lower_third", "callout", "source"),
        graphic_density=0.5,
        caption_preset="documentary",
        music_intensity=0.15,
        music_moods=("neutral", "calm"),
        research_depth="primary statements",
        source_requirement=2,
        editorial_notes=(
            "Keep direct quotes verbatim and visually distinct from paraphrase.",
            "Never invent a speaker's role or affiliation.",
        ),
    ),
    "local_regional": _genre(
        "local_regional",
        "Local & Regional News",
        "Location-specific visuals, local context, maps, and practical information.",
        "Practical narration with place names, local maps, and service information.",
        "broadcast",
        script_pacing="measured",
        asset_priority=("photograph", "map", "broll", "document"),
        footage_ratio=0.4,
        shot_mix=("wide-map", "medium", "close-label"),
        motion="slow_pan",
        motion_intensity=0.28,
        transition="fade",
        graphic_templates=("map", "lower_third", "stat", "source"),
        graphic_density=0.55,
        caption_preset="professional_news",
        music_intensity=0.25,
        music_moods=("calm", "neutral", "editorial"),
        research_depth="local context",
        source_requirement=2,
        editorial_notes=(
            "Name the neighbourhood and the relevant authority.",
            "Include practical details such as closures or helplines only when verified.",
        ),
    ),
}

GENRE_ORDER: tuple[str, ...] = tuple(GENRES)

# Terms used only to recommend a genre when the user chooses Auto. They are
# matched against the story title and script, never used to change facts.
AUTO_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "disaster_humanitarian",
        (
            "earthquake",
            "flood",
            "hurricane",
            "wildfire",
            "disaster",
            "refugee",
            "humanitarian",
            "famine",
            "cyclone",
            "tsunami",
            "train crash",
            "derailment",
            "derailed",
            "crash",
            "casualties",
            "rescue",
        ),
    ),
    (
        "crime_investigation",
        (
            "police",
            "court",
            "arrest",
            "charged",
            "investigation",
            "murder",
            "fraud",
            "trial",
        ),
    ),
    (
        "business_economy",
        (
            "market",
            "economy",
            "inflation",
            "gdp",
            "stocks",
            "revenue",
            "bank",
            "trade",
            "tariff",
            "company",
            "earnings",
        ),
    ),
    (
        "technology_ai",
        (
            "ai",
            "artificial intelligence",
            "software",
            "chip",
            "semiconductor",
            "cyber",
            "robot",
            "algorithm",
            "startup",
            "platform",
        ),
    ),
    (
        "science_research",
        (
            "study",
            "research",
            "scientists",
            "climate",
            "space",
            "nasa",
            "vaccine",
            "experiment",
            "peer-reviewed",
        ),
    ),
    (
        "world_geopolitics",
        (
            "border",
            "diplomatic",
            "sanctions",
            "geopolitical",
            "foreign minister",
            "nato",
            "united nations",
            "war",
            "ceasefire",
            "truce",
            "invasion",
        ),
    ),
    (
        "politics_government",
        (
            "parliament",
            "congress",
            "senate",
            "election",
            "minister",
            "policy",
            "government",
            "president",
            "legislation",
        ),
    ),
    (
        "sports_news",
        (
            "league",
            "tournament",
            "coach",
            "goal",
            "olympic",
            "cricket",
            "football",
            "tennis",
        ),
    ),
    (
        "entertainment_celebrity",
        (
            "film",
            "actor",
            "singer",
            "album",
            "award",
            "celebrity",
            "box office",
            "tv series",
            "streaming",
        ),
    ),
    (
        "local_regional",
        (
            "city council",
            "local",
            "neighbourhood",
            "neighborhood",
            "county",
            "district",
            "community",
            "residents",
        ),
    ),
    (
        "interview_quote",
        ("interview", "said in an interview", "told reporters", "statement", "spokesperson"),
    ),
    ("data_journalism", ("survey", "dataset", "analysis of", "percent", "statistics", "poll")),
    ("explainer_educational", ("explained", "how does", "what is", "guide", "steps", "why")),
    ("breaking_news", ("breaking", "live update", "just in", "developing")),
)


@dataclass(frozen=True)
class GenreRecommendation:
    genre: GenreProfile
    rationale: str
    automatic: bool = True


@lru_cache(maxsize=256)
def _hint_pattern(term: str) -> re.Pattern[str]:
    """Match a hint as whole words so ``ai`` never fires inside ``said``."""

    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


# Question stems and generic explainer words appear in almost every script.
# They are still useful, but must not outrank a real subject match such as
# "ceasefire" or "truce".
_GENERIC_HINTS = frozenset(
    {
        "what is",
        "how does",
        "guide",
        "steps",
        "explained",
        "explainer",
        "what we know",
    }
)


def recommend_genre(text: str) -> GenreRecommendation:
    """Infer a suitable profile from the story text, with a short rationale.

    Hints are matched as whole words and scored, so a story that mentions a
    ceasefire outranks one that merely contains the letters ``ai`` inside
    another word. Longer phrases count for more than single words, and generic
    question stems are discounted.
    """

    haystack = " ".join((strip_meta_sentences(text) or "").split())
    best_score = 0.0
    best_key: str | None = None
    best_matched: list[str] = []
    for key, terms in AUTO_HINTS:
        matched: list[str] = []
        score = 0.0
        for term in terms:
            count = len(_hint_pattern(term).findall(haystack))
            if count:
                matched.append(term)
                words = term.count(" ") + 1
                weight = 1.0 + 0.5 * (words - 1)
                if term in _GENERIC_HINTS:
                    weight *= 0.4
                score += count * weight
        if matched and score > best_score:
            best_score = score
            best_key = key
            best_matched = matched
    if best_key is not None:
        genre = GENRES[best_key]
        quoted = ", ".join(f'"{term}"' for term in best_matched[:3])
        return GenreRecommendation(
            genre=genre,
            rationale=f"Recommended {genre.name}: the story mentions {quoted}.",
        )
    return GenreRecommendation(
        genre=GENRES["general_news"],
        rationale=(
            "Recommended General News: no specialised subject signal was strong "
            "enough, so the balanced newsroom profile is the safest default."
        ),
    )


def resolve_genre(
    key: str | None,
    *,
    story_text: str = "",
    explicit: bool = False,
) -> GenreRecommendation:
    """Resolve an explicit genre or recommend one for Auto.

    ``explicit`` records whether the operator actually chose the key. A caller
    that falls back to a default must not present that as an automatic
    recommendation.
    """

    if key and key in GENRES:
        return GenreRecommendation(
            genre=GENRES[key],
            rationale=f"Operator selected {GENRES[key].name}.",
            automatic=False,
        )
    if key and key != AUTO_GENRE:
        raise ValueError(f"Unknown news genre: {key}")
    recommendation = recommend_genre(story_text)
    if explicit:
        return recommendation
    return recommendation


@dataclass(frozen=True)
class EffectiveProductionProfile:
    """The resolved combination of genre, presentation style, and overrides."""

    genre: GenreProfile
    preset: EditingPreset
    caption_preset: str
    motion_intensity: float
    target_lufs: float
    music_enabled: bool
    music_level: float
    narration_level: float
    sfx_enabled: bool
    sfx_level: float
    style_overridden: bool
    rationale: str
    overrides: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "genre_key": self.genre.key,
            "genre_name": self.genre.name,
            "genre": self.genre.to_public_dict(),
            "style_key": self.preset.key,
            "style_name": self.preset.name,
            "style_overridden": self.style_overridden,
            "caption_preset": self.caption_preset,
            "motion_intensity": self.motion_intensity,
            "audio": {
                "target_lufs": self.target_lufs,
                "music_enabled": self.music_enabled,
                "music_level": self.music_level,
                "narration_level": self.narration_level,
                "sfx_enabled": self.sfx_enabled,
                "sfx_level": self.sfx_level,
            },
            "rationale": self.rationale,
            "overrides": self.overrides,
        }


def build_effective_profile(
    genre_key: str | None,
    *,
    style_key: str | None = None,
    story_text: str = "",
    overrides: dict[str, Any] | None = None,
) -> EffectiveProductionProfile:
    """Resolve genre + style + user overrides into one shared profile.

    The genre supplies defaults. The editing style is a presentation override:
    when the operator chooses one it wins, otherwise the genre's default style
    is used. Audio and motion overrides are applied last and are clamped to
    supported ranges.
    """

    overrides = dict(overrides or {})
    recommendation = resolve_genre(genre_key, story_text=story_text)
    genre = recommendation.genre
    explicit_style = (style_key or "").strip()
    if explicit_style and explicit_style not in {"", AUTO_GENRE, "genre_default"}:
        preset = get_preset(explicit_style)
        style_overridden = explicit_style != genre.default_style
    else:
        preset = get_preset(genre.default_style)
        style_overridden = False

    motion_label = str(overrides.get("motion_intensity") or "").strip().lower()
    if motion_label in MOTION_INTENSITY_LABELS:
        motion_multiplier = MOTION_INTENSITY_LABELS[motion_label]
    else:
        try:
            motion_multiplier = float(overrides.get("motion_scale", 1.0))
        except (TypeError, ValueError):
            motion_multiplier = 1.0
    motion_intensity = round(max(0.0, min(1.0, genre.motion_intensity * motion_multiplier)), 3)

    transition = str(overrides.get("transition") or genre.transition)
    graphic_templates = tuple(genre.graphic_templates)
    if overrides.get("graphics_enabled") is False:
        graphic_templates = ()

    craft = replace(
        preset.craft,
        shot_mix=tuple(genre.shot_mix),
        cut_rhythm=genre.script_pacing,
        transition_guidance=(f"{genre.name}: {genre.default_approach}"),
        music_gain=round(min(0.16, max(0.0, genre.music_intensity * 0.22)), 4)
        if genre.music_enabled
        else 0.0,
        ducking=genre.ducking,
        sfx_gain=round(min(0.08, max(0.0, genre.sfx_intensity * 0.2)), 4)
        if genre.sfx_enabled
        else 0.0,
    )
    preset = replace(
        preset,
        craft=craft,
        motion=genre.motion,
        motion_intensity=motion_intensity,
        transition=transition if transition else preset.transition,
        asset_priority=tuple(genre.asset_priority),
        graphics=graphic_templates,
        lower_third=genre.lower_third,
        ticker=genre.ticker,
    )

    caption_preset = str(overrides.get("caption_preset") or genre.caption_preset)
    try:
        target_lufs = float(overrides.get("target_lufs", genre.target_lufs))
    except (TypeError, ValueError):
        target_lufs = genre.target_lufs
    target_lufs = max(-24.0, min(-12.0, target_lufs))

    def _level(name: str, default: float) -> float:
        try:
            value = float(overrides.get(name, default))
        except (TypeError, ValueError):
            value = default
        return round(max(0.0, min(1.0, value)), 3)

    music_enabled = bool(overrides.get("music_enabled", genre.music_enabled))
    sfx_enabled = bool(overrides.get("sfx_enabled", genre.sfx_enabled))
    # Sensitive reporting never gets decorative sound design, even if a caller
    # asks for it through a generic override.
    if genre.sensitivity in {"sensitive", "high"}:
        music_enabled = False
        sfx_enabled = False

    applied = {key: value for key, value in overrides.items() if value is not None}
    return EffectiveProductionProfile(
        genre=genre,
        preset=preset,
        caption_preset=caption_preset,
        motion_intensity=motion_intensity,
        target_lufs=target_lufs,
        music_enabled=music_enabled,
        music_level=_level("music_level", genre.music_intensity),
        narration_level=_level("narration_level", 1.0),
        sfx_enabled=sfx_enabled,
        sfx_level=_level("sfx_level", genre.sfx_intensity),
        style_overridden=style_overridden,
        rationale=recommendation.rationale,
        overrides=applied,
    )


def list_genres() -> list[dict[str, Any]]:
    return [genre.to_public_dict() for genre in GENRES.values()]


def genre_summary(profile: EffectiveProductionProfile) -> dict[str, Any]:
    """Compact payload for the dashboard job header."""

    return {
        "genre_key": profile.genre.key,
        "genre_name": profile.genre.name,
        "description": profile.genre.description,
        "default_approach": profile.genre.default_approach,
        "style_key": profile.preset.key,
        "style_name": profile.preset.name,
        "style_overridden": profile.style_overridden,
        "caption_preset": profile.caption_preset,
        "motion_intensity": profile.motion_intensity,
        "music_enabled": profile.music_enabled,
        "sfx_enabled": profile.sfx_enabled,
        "sensitivity": profile.genre.sensitivity,
        "rationale": profile.rationale,
    }
