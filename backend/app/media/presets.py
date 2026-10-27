from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

DEFAULT_PRESET_KEY = "broadcast"


@dataclass(frozen=True)
class CraftRules:
    """Editing-craft rules derived from the video-editor guide and applied in code."""

    narrative_structure: str
    shot_mix: tuple[str, ...]
    cut_rhythm: str
    transition_guidance: str
    color_grade: str
    exposure: float
    contrast: float
    saturation: float
    temperature: float
    music_gain: float
    ducking: bool
    sfx_gain: float
    caption_priority: str
    platform_fit: tuple[str, ...]
    safe_area_note: str
    easing: str = "ease_in_out"
    motion_path: str = "linear"
    source_note: str = (
        "Craft rules synthesized from 'How to Become The Video Editor' "
        "(cuts, A/B-roll, transitions, keyframes, color, music, story flow, social formats)."
    )



@dataclass(frozen=True)
class Palette:
    """A small, deliberately restrained palette used by every generated frame."""

    background: str
    surface: str
    accent: str
    secondary: str
    text: str
    muted: str


@dataclass(frozen=True)
class LayoutRules:
    """Aspect-specific composition rules, not a stretch of the other format."""

    headline_position: str = "top"
    headline_align: str = "left"
    caption_position: str = "bottom"
    safe_margin_ratio: float = 0.055
    graphic_position: str = "right"
    image_fit: str = "cover"
    max_headline_chars: int = 46


@dataclass(frozen=True)
class EditingPreset:
    """A complete, reusable editing style that changes actual rendered output."""

    key: str
    name: str
    description: str
    best_for: str
    palette: Palette
    heading_font: str
    body_font: str
    headline_scale: float
    caption_scale: float
    kicker_scale: float
    caption_style: str
    transition: str
    motion: str
    motion_intensity: float
    shot_seconds: float
    asset_priority: tuple[str, ...]
    graphics: tuple[str, ...]
    music: str
    sfx: str
    lower_third: bool
    opening: str
    closing: str
    landscape: LayoutRules = field(default_factory=LayoutRules)
    portrait: LayoutRules = field(
        default_factory=lambda: LayoutRules(
            headline_position="top",
            caption_position="bottom",
            safe_margin_ratio=0.075,
            graphic_position="bottom",
            max_headline_chars=28,
        )
    )
    ticker: bool = False
    show_source: bool = True
    grain: float = 0.0
    vignette: float = 0.0
    accent_rule: str = "bar"
    craft: CraftRules = field(
        default_factory=lambda: CraftRules(
            narrative_structure="hook-development-resolution",
            shot_mix=("wide", "medium", "close"),
            cut_rhythm="measured",
            transition_guidance="Use cuts by default; reserve fades for time or location changes.",
            color_grade="neutral",
            exposure=1.0,
            contrast=1.05,
            saturation=1.0,
            temperature=0.0,
            music_gain=0.08,
            ducking=True,
            sfx_gain=0.0,
            caption_priority="medium",
            platform_fit=("16:9", "9:16"),
            safe_area_note="Keep captions inside the lower safe area and clear of platform UI.",
        )
    )

    def to_public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["asset_priority"] = list(self.asset_priority)
        payload["graphics"] = list(self.graphics)
        return payload


def _palette(
    background: str,
    surface: str,
    accent: str,
    secondary: str,
    text: str = "#F8FAFC",
    muted: str = "#94A3B8",
) -> Palette:
    return Palette(
        background=background,
        surface=surface,
        accent=accent,
        secondary=secondary,
        text=text,
        muted=muted,
    )


def _landscape(
    *,
    headline_position: str = "top",
    headline_align: str = "left",
    graphic_position: str = "right",
    safe: float = 0.055,
    max_chars: int = 46,
    image_fit: str = "cover",
) -> LayoutRules:
    return LayoutRules(
        headline_position=headline_position,
        headline_align=headline_align,
        caption_position="bottom",
        safe_margin_ratio=safe,
        graphic_position=graphic_position,
        image_fit=image_fit,
        max_headline_chars=max_chars,
    )


def _portrait(
    *,
    headline_position: str = "top",
    graphic_position: str = "bottom",
    safe: float = 0.075,
    max_chars: int = 28,
    image_fit: str = "cover",
) -> LayoutRules:
    return LayoutRules(
        headline_position=headline_position,
        headline_align="left",
        caption_position="bottom",
        safe_margin_ratio=safe,
        graphic_position=graphic_position,
        image_fit=image_fit,
        max_headline_chars=max_chars,
    )


PRESETS: dict[str, EditingPreset] = {
    "broadcast": EditingPreset(
        key="broadcast",
        name="Broadcast News",
        description=(
            "Conventional newsroom language: clear headlines, lower-thirds, location "
            "labels, restrained transitions, and a composed presenter-led pace."
        ),
        best_for="General news, interviews, explainers, and daily summaries.",
        palette=_palette("#0B1220", "#111C2E", "#38BDF8", "#F59E0B"),
        heading_font="arialbd.ttf",
        body_font="arial.ttf",
        headline_scale=0.072,
        caption_scale=0.030,
        kicker_scale=0.022,
        caption_style="lower_third",
        transition="fade",
        motion="slow_zoom",
        motion_intensity=0.35,
        shot_seconds=7.0,
        asset_priority=("photograph", "broll", "map", "document"),
        graphics=("lower_third", "location", "source", "stat"),
        music="subtle",
        sfx="minimal",
        lower_third=True,
        opening="headline_band",
        closing="source_card",
        landscape=_landscape(),
        portrait=_portrait(),
        ticker=True,
    ),
    "vox": EditingPreset(
        key="vox",
        name="VOX-Inspired Explainer",
        description=(
            "Explanatory graphics first: animated maps, cited figures, timelines, "
            "diagram labels, and evidence callouts synchronized to the narration."
        ),
        best_for="Economics, politics, technology, science, and public policy.",
        palette=_palette("#F7F3EA", "#FFFFFF", "#E4572E", "#1D3557", "#111827", "#5B6472"),
        heading_font="arialbd.ttf",
        body_font="arial.ttf",
        headline_scale=0.078,
        caption_scale=0.028,
        kicker_scale=0.021,
        caption_style="boxed",
        transition="smoothleft",
        motion="parallax",
        motion_intensity=0.5,
        shot_seconds=8.0,
        asset_priority=("map", "chart", "diagram", "document", "photograph"),
        graphics=("map", "chart", "timeline", "stat", "diagram", "source", "label"),
        music="ambient",
        sfx="restrained",
        lower_third=False,
        opening="question_card",
        closing="source_card",
        landscape=_landscape(headline_position="top", graphic_position="right", max_chars=44),
        portrait=_portrait(headline_position="top", graphic_position="bottom", max_chars=26),
        accent_rule="underline",
    ),
    "documentary": EditingPreset(
        key="documentary",
        name="Documentary & Investigative",
        description=(
            "Longer shots, archival material, document callouts, timelines, and a clear "
            "separation between confirmed facts, allegations, and open questions."
        ),
        best_for="Investigations, historical events, and long-form reporting.",
        palette=_palette("#0E0D0B", "#1C1916", "#D4A373", "#8A9A5B", "#F5F1E8", "#A8A29E"),
        heading_font="georgiab.ttf",
        body_font="georgia.ttf",
        headline_scale=0.064,
        caption_scale=0.027,
        kicker_scale=0.020,
        caption_style="minimal",
        transition="dissolve",
        motion="slow_pan",
        motion_intensity=0.25,
        shot_seconds=11.0,
        asset_priority=("archival", "document", "photograph", "broll", "timeline"),
        graphics=("source", "timeline", "evidence", "label"),
        music="tense",
        sfx="restrained",
        lower_third=True,
        opening="title_card",
        closing="evidence_note",
        landscape=_landscape(safe=0.06),
        portrait=_portrait(safe=0.08),
        grain=0.06,
        vignette=0.35,
        accent_rule="line",
    ),
    "cinematic": EditingPreset(
        key="cinematic",
        name="Cinematic News",
        description=(
            "Carefully selected B-roll, controlled contrast, deliberate camera moves, "
            "and sound design without dramatizing beyond the evidence."
        ),
        best_for="Major events, environment, humanitarian, and feature reporting.",
        palette=_palette("#05070A", "#10151C", "#E2B36A", "#7AA2C8", "#F4F4F5", "#9CA3AF"),
        heading_font="arialbd.ttf",
        body_font="arial.ttf",
        headline_scale=0.068,
        caption_scale=0.026,
        kicker_scale=0.019,
        caption_style="minimal",
        transition="fade",
        motion="parallax",
        motion_intensity=0.65,
        shot_seconds=9.0,
        asset_priority=("broll", "photograph", "archival", "map"),
        graphics=("source", "location", "label"),
        music="ambient",
        sfx="cinematic",
        lower_third=False,
        opening="cold_open",
        closing="title_card",
        landscape=_landscape(safe=0.05),
        portrait=_portrait(safe=0.07),
        grain=0.03,
        vignette=0.5,
        accent_rule="line",
    ),
    "data": EditingPreset(
        key="data",
        name="Data-Driven News",
        description=(
            "Cited figures, comparisons, and timelines generated from numbers present "
            "in the narration. No values are invented for visual effect."
        ),
        best_for="Economy, elections, markets, demographics, climate, and statistics.",
        palette=_palette("#08131F", "#0F2233", "#22D3EE", "#A3E635", "#F0FDFA", "#7DD3FC"),
        heading_font="consolab.ttf",
        body_font="consola.ttf",
        headline_scale=0.066,
        caption_scale=0.027,
        kicker_scale=0.020,
        caption_style="boxed",
        transition="wipeleft",
        motion="static",
        motion_intensity=0.15,
        shot_seconds=8.0,
        asset_priority=("chart", "document", "map", "photograph"),
        graphics=("chart", "stat", "source", "timeline", "label"),
        music="ambient",
        sfx="minimal",
        lower_third=False,
        opening="stat_card",
        closing="source_card",
        landscape=_landscape(graphic_position="right", max_chars=48),
        portrait=_portrait(graphic_position="bottom", max_chars=26),
        accent_rule="grid",
    ),
    "map_geopolitical": EditingPreset(
        key="map_geopolitical",
        name="Map & Geopolitical Explainer",
        description=(
            "Location-first compositions with a clearly illustrative map, markers, "
            "routes, and labels that distinguish known locations from uncertainty."
        ),
        best_for="International affairs, conflicts, disasters, and regional news.",
        palette=_palette("#071A2B", "#0C2C46", "#F4A261", "#2A9D8F", "#F1FAEE", "#94A3B8"),
        heading_font="arialbd.ttf",
        body_font="arial.ttf",
        headline_scale=0.070,
        caption_scale=0.028,
        kicker_scale=0.021,
        caption_style="lower_third",
        transition="slideleft",
        motion="map_drift",
        motion_intensity=0.45,
        shot_seconds=9.0,
        asset_priority=("map", "photograph", "document", "broll"),
        graphics=("map", "location", "label", "timeline", "source"),
        music="tense",
        sfx="restrained",
        lower_third=True,
        opening="locator",
        closing="source_card",
        landscape=_landscape(graphic_position="right", max_chars=46),
        portrait=_portrait(graphic_position="bottom", max_chars=26),
        accent_rule="line",
    ),
    "social": EditingPreset(
        key="social",
        name="Social Media News",
        description=(
            "Vertical-first, strong opening, concise headlines, large readable captions, "
            "and faster pacing while preserving context and verified facts."
        ),
        best_for="Shorts, Reels, TikTok, and social distribution.",
        palette=_palette("#09090B", "#18181B", "#F43F5E", "#FACC15", "#FAFAFA", "#A1A1AA"),
        heading_font="impact.ttf",
        body_font="arialbd.ttf",
        headline_scale=0.085,
        caption_scale=0.040,
        kicker_scale=0.026,
        caption_style="centered",
        transition="slideup",
        motion="punch",
        motion_intensity=0.8,
        shot_seconds=4.5,
        asset_priority=("photograph", "broll", "stat", "map"),
        graphics=("stat", "label", "source", "headline"),
        music="uplifting",
        sfx="minimal",
        lower_third=False,
        opening="hook",
        closing="source_card",
        landscape=_landscape(headline_position="center", max_chars=40),
        portrait=_portrait(headline_position="top", max_chars=24),
        accent_rule="bar",
    ),
    "minimal": EditingPreset(
        key="minimal",
        name="Minimal Editorial",
        description=(
            "Restrained typography, generous negative space, concise captions, and "
            "subtle motion for stories where clarity matters more than spectacle."
        ),
        best_for="Serious news, business, and public-interest reporting.",
        palette=_palette("#FAFAF9", "#FFFFFF", "#1C1917", "#B91C1C", "#1C1917", "#78716C"),
        heading_font="georgiab.ttf",
        body_font="georgia.ttf",
        headline_scale=0.060,
        caption_scale=0.026,
        kicker_scale=0.019,
        caption_style="minimal",
        transition="fade",
        motion="static",
        motion_intensity=0.1,
        shot_seconds=10.0,
        asset_priority=("photograph", "document", "map"),
        graphics=("source", "label"),
        music="none",
        sfx="none",
        lower_third=False,
        opening="title_card",
        closing="source_card",
        landscape=_landscape(headline_position="bottom", max_chars=50),
        portrait=_portrait(headline_position="top", max_chars=28),
        accent_rule="line",
    ),
    "photojournalism": EditingPreset(
        key="photojournalism",
        name="Photojournalism & B-Roll",
        description=(
            "Authentic reporting imagery leads, with subtle pans, zooms, crops, and "
            "context labels. Illustrative visuals are identified as such."
        ),
        best_for="Stories where photography and footage provide the strongest context.",
        palette=_palette("#0C0A09", "#1C1917", "#FB923C", "#FDE68A", "#FAFAF9", "#A8A29E"),
        heading_font="arialbd.ttf",
        body_font="arial.ttf",
        headline_scale=0.062,
        caption_scale=0.027,
        kicker_scale=0.020,
        caption_style="lower_third",
        transition="dissolve",
        motion="ken_burns",
        motion_intensity=0.45,
        shot_seconds=8.0,
        asset_priority=("photograph", "broll", "archival", "document"),
        graphics=("location", "source", "label"),
        music="subtle",
        sfx="restrained",
        lower_third=True,
        opening="photo_open",
        closing="source_card",
        landscape=_landscape(safe=0.045),
        portrait=_portrait(safe=0.07),
        grain=0.02,
        vignette=0.2,
    ),
    "split_screen": EditingPreset(
        key="split_screen",
        name="Split-Screen Comparison",
        description=(
            "Two-panel comparisons of places, periods, statements, or series with "
            "equivalent framing, units, and labels where the evidence allows it."
        ),
        best_for="Policy comparisons, historical change, and data-led reporting.",
        palette=_palette("#0F172A", "#1E293B", "#60A5FA", "#F472B6", "#F8FAFC", "#94A3B8"),
        heading_font="arialbd.ttf",
        body_font="arial.ttf",
        headline_scale=0.062,
        caption_scale=0.027,
        kicker_scale=0.020,
        caption_style="boxed",
        transition="wiperight",
        motion="static",
        motion_intensity=0.2,
        shot_seconds=9.0,
        asset_priority=("photograph", "chart", "document", "map"),
        graphics=("split", "label", "stat", "source"),
        music="subtle",
        sfx="minimal",
        lower_third=False,
        opening="comparison",
        closing="source_card",
        landscape=_landscape(graphic_position="center", max_chars=50),
        portrait=_portrait(graphic_position="bottom", max_chars=28),
        accent_rule="split",
    ),
    "timeline": EditingPreset(
        key="timeline",
        name="Timeline & Historical Reconstruction",
        description=(
            "Dates, events, archival material, and document excerpts connected on a "
            "timeline that separates confirmed records from interpretation."
        ),
        best_for="Historical news, investigations, legal proceedings, and developing stories.",
        palette=_palette("#14110F", "#211D19", "#F59E0B", "#93C5FD", "#FEF3C7", "#A8A29E"),
        heading_font="georgiab.ttf",
        body_font="georgia.ttf",
        headline_scale=0.060,
        caption_scale=0.026,
        kicker_scale=0.019,
        caption_style="minimal",
        transition="slideleft",
        motion="slow_pan",
        motion_intensity=0.3,
        shot_seconds=10.0,
        asset_priority=("archival", "document", "timeline", "photograph"),
        graphics=("timeline", "source", "evidence", "label"),
        music="tense",
        sfx="restrained",
        lower_third=True,
        opening="timeline_open",
        closing="evidence_note",
        landscape=_landscape(graphic_position="bottom", max_chars=48),
        portrait=_portrait(graphic_position="bottom", max_chars=28),
        grain=0.04,
        accent_rule="line",
    ),
    "kinetic_type": EditingPreset(
        key="kinetic_type",
        name="Kinetic Typography",
        description=(
            "Important words, figures, and short quotations animate in sync with the "
            "narration, at a readable pace and without altering quoted meaning."
        ),
        best_for="Short explainers, quote-led stories, and statistics.",
        palette=_palette("#020617", "#0F172A", "#FDE047", "#F472B6", "#F8FAFC", "#94A3B8"),
        heading_font="impact.ttf",
        body_font="arialbd.ttf",
        headline_scale=0.110,
        caption_scale=0.034,
        kicker_scale=0.024,
        caption_style="kinetic",
        transition="radial",
        motion="punch",
        motion_intensity=0.9,
        shot_seconds=4.0,
        asset_priority=("typography", "stat", "photograph", "chart"),
        graphics=("headline", "stat", "quote", "label"),
        music="uplifting",
        sfx="minimal",
        lower_third=False,
        opening="word_hit",
        closing="source_card",
        landscape=_landscape(headline_position="center", max_chars=36),
        portrait=_portrait(headline_position="center", max_chars=22),
        accent_rule="bar",
    ),
    "breaking": EditingPreset(
        key="breaking",
        name="Live-Update & Breaking News",
        description=(
            "Timestamped update bands, location markers, and concise panels. The "
            "breaking label is only used when the story's timing justifies it."
        ),
        best_for="Verified developing stories and live updates.",
        palette=_palette("#0B0B0D", "#17171B", "#EF4444", "#F59E0B", "#FAFAFA", "#A1A1AA"),
        heading_font="arialbd.ttf",
        body_font="arial.ttf",
        headline_scale=0.070,
        caption_scale=0.030,
        kicker_scale=0.022,
        caption_style="lower_third",
        transition="cut",
        motion="static",
        motion_intensity=0.1,
        shot_seconds=6.0,
        asset_priority=("photograph", "map", "document", "broll"),
        graphics=("ticker", "location", "source", "label", "timeline"),
        music="none",
        sfx="minimal",
        lower_third=True,
        opening="update_band",
        closing="source_card",
        landscape=_landscape(headline_position="bottom", max_chars=50),
        portrait=_portrait(headline_position="top", max_chars=28),
        ticker=True,
        accent_rule="band",
    ),
    "interview": EditingPreset(
        key="interview",
        name="Interview & Quote-Led",
        description=(
            "Speaker identification, quotation cards, context captions, and supporting "
            "B-roll. Direct quotes are kept distinct from paraphrase."
        ),
        best_for="Interviews, statements, and accountability reporting.",
        palette=_palette("#101014", "#1B1B22", "#C084FC", "#FBBF24", "#FAFAFA", "#A1A1AA"),
        heading_font="georgiab.ttf",
        body_font="arial.ttf",
        headline_scale=0.060,
        caption_scale=0.027,
        kicker_scale=0.020,
        caption_style="lower_third",
        transition="dissolve",
        motion="slow_zoom",
        motion_intensity=0.25,
        shot_seconds=9.0,
        asset_priority=("photograph", "broll", "document", "archival"),
        graphics=("quote", "lower_third", "source", "label"),
        music="subtle",
        sfx="none",
        lower_third=True,
        opening="speaker_card",
        closing="source_card",
        landscape=_landscape(graphic_position="right", max_chars=46),
        portrait=_portrait(graphic_position="bottom", max_chars=26),
        accent_rule="quote",
    ),
    "diagram": EditingPreset(
        key="diagram",
        name="Motion-Graphic Diagram Explainer",
        description=(
            "Diagrams, labeled components, flow arrows, and step-by-step reveals that "
            "faithfully represent the underlying information."
        ),
        best_for="Technology, infrastructure, science, and complex systems.",
        palette=_palette("#0B1120", "#111C33", "#34D399", "#60A5FA", "#ECFDF5", "#94A3B8"),
        heading_font="consolab.ttf",
        body_font="consola.ttf",
        headline_scale=0.064,
        caption_scale=0.026,
        kicker_scale=0.019,
        caption_style="boxed",
        transition="circleopen",
        motion="parallax",
        motion_intensity=0.4,
        shot_seconds=9.0,
        asset_priority=("diagram", "document", "chart", "photograph"),
        graphics=("diagram", "label", "source", "stat"),
        music="ambient",
        sfx="minimal",
        lower_third=False,
        opening="system_map",
        closing="source_card",
        landscape=_landscape(graphic_position="right", max_chars=48),
        portrait=_portrait(graphic_position="bottom", max_chars=26),
        accent_rule="grid",
    ),
    "archival": EditingPreset(
        key="archival",
        name="Archival & Historical News",
        description=(
            "Permitted historical images, document scans, date labels, and provenance "
            "notes that make clear when footage depicts an earlier period."
        ),
        best_for="Historical context and archive-led reporting.",
        palette=_palette("#17130F", "#241E18", "#D6B98C", "#A3B18A", "#F5EFE6", "#A8A29E"),
        heading_font="georgiab.ttf",
        body_font="georgia.ttf",
        headline_scale=0.060,
        caption_scale=0.026,
        kicker_scale=0.019,
        caption_style="minimal",
        transition="dissolve",
        motion="slow_pan",
        motion_intensity=0.2,
        shot_seconds=11.0,
        asset_priority=("archival", "document", "photograph", "timeline"),
        graphics=("timeline", "source", "evidence", "label"),
        music="none",
        sfx="none",
        lower_third=True,
        opening="archive_slate",
        closing="provenance",
        landscape=_landscape(safe=0.06),
        portrait=_portrait(safe=0.08),
        grain=0.09,
        vignette=0.4,
        accent_rule="line",
    ),
    "magazine": EditingPreset(
        key="magazine",
        name="News Magazine",
        description=(
            "A presenter-led opening, varied pacing, photography, graphics, interviews, "
            "and longer feature segments for weekly or roundup formats."
        ),
        best_for="Longer features and weekly news roundups.",
        palette=_palette("#12100E", "#1F1B17", "#E76F51", "#E9C46A", "#FDF6EC", "#A8A29E"),
        heading_font="georgiab.ttf",
        body_font="georgia.ttf",
        headline_scale=0.066,
        caption_scale=0.027,
        kicker_scale=0.020,
        caption_style="lower_third",
        transition="smoothleft",
        motion="ken_burns",
        motion_intensity=0.4,
        shot_seconds=10.0,
        asset_priority=("photograph", "broll", "document", "chart", "map"),
        graphics=("lower_third", "source", "stat", "quote", "label"),
        music="subtle",
        sfx="restrained",
        lower_third=True,
        opening="presenter_open",
        closing="source_card",
        landscape=_landscape(max_chars=46),
        portrait=_portrait(max_chars=28),
        accent_rule="bar",
    ),
    "audio_led": EditingPreset(
        key="audio_led",
        name="Audio-Led Visual Storytelling",
        description=(
            "Narration structures the sequence; photographs, restrained animation, "
            "captions, and contextual graphics change with the spoken content."
        ),
        best_for="Strong narration without continuous video footage.",
        palette=_palette("#0A0A0A", "#161616", "#E5E5E5", "#A3A3A3", "#FAFAFA", "#737373"),
        heading_font="georgiab.ttf",
        body_font="georgia.ttf",
        headline_scale=0.058,
        caption_scale=0.028,
        kicker_scale=0.019,
        caption_style="minimal",
        transition="fade",
        motion="static",
        motion_intensity=0.08,
        shot_seconds=12.0,
        asset_priority=("photograph", "document", "archival", "map"),
        graphics=("source", "label", "quote"),
        music="none",
        sfx="none",
        lower_third=False,
        opening="title_card",
        closing="source_card",
        landscape=_landscape(headline_position="bottom", max_chars=50),
        portrait=_portrait(headline_position="top", max_chars=28),
        accent_rule="line",
    ),
}


CRAFT_RULES: dict[str, CraftRules] = {
    "broadcast": CraftRules(
        narrative_structure="hook → development → verified resolution",
        shot_mix=("wide", "medium", "close", "evidence"),
        cut_rhythm="measured",
        transition_guidance=(
            "Hard cuts carry the report; use a short fade only when time or location changes."
        ),
        color_grade="neutral",
        exposure=1.02,
        contrast=1.08,
        saturation=1.02,
        temperature=0.05,
        music_gain=0.09,
        ducking=True,
        sfx_gain=0.02,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "broadcast", "web"),
        safe_area_note="Lower-third and captions sit above the bottom 8% of frame.",
    ),
    "vox": CraftRules(
        narrative_structure="question → explanation → evidence → implication",
        shot_mix=("wide-map", "medium-graphic", "close-stat", "evidence"),
        cut_rhythm="beat",
        transition_guidance=(
            "Use explanatory wipes or smooth slides only when the graphic relationship changes."
        ),
        color_grade="warm-neutral",
        exposure=1.03,
        contrast=1.05,
        saturation=0.95,
        temperature=0.08,
        music_gain=0.08,
        ducking=True,
        sfx_gain=0.03,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "web explainer"),
        safe_area_note="Keep map labels and chart axes inside a 6% title-safe margin.",
    ),
    "documentary": CraftRules(
        narrative_structure="hook → context → evidence → unresolved questions",
        shot_mix=("wide", "medium", "close", "document"),
        cut_rhythm="lingering",
        transition_guidance=(
            "Prefer dissolves for archival time shifts; avoid decorative wipes on evidence."
        ),
        color_grade="desaturated-warm",
        exposure=0.98,
        contrast=1.12,
        saturation=0.82,
        temperature=0.12,
        music_gain=0.07,
        ducking=True,
        sfx_gain=0.02,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "documentary"),
        safe_area_note="Document edges stay inside 7% margins; never crop source text.",
    ),
    "cinematic": CraftRules(
        narrative_structure="hook → development → climax → resolution",
        shot_mix=("wide", "medium", "close"),
        cut_rhythm="measured",
        transition_guidance=(
            "Use long fades between acts and hard cuts inside a sequence; avoid excess motion."
        ),
        color_grade="high-contrast-cool",
        exposure=0.96,
        contrast=1.18,
        saturation=0.95,
        temperature=-0.08,
        music_gain=0.12,
        ducking=True,
        sfx_gain=0.04,
        caption_priority="low",
        platform_fit=("YouTube 16:9", "feature"),
        safe_area_note="Keep faces and key evidence away from the outer 5% edge.",
    ),
    "data": CraftRules(
        narrative_structure="finding → evidence → comparison → implication",
        shot_mix=("wide", "medium-chart", "close-stat", "evidence"),
        cut_rhythm="beat",
        transition_guidance=(
            "Use a wipe only when moving between datasets; otherwise cut on the spoken figure."
        ),
        color_grade="neutral-cool",
        exposure=1.02,
        contrast=1.05,
        saturation=0.9,
        temperature=-0.05,
        music_gain=0.06,
        ducking=True,
        sfx_gain=0.01,
        caption_priority="high",
        platform_fit=("YouTube 16:9", "data explainer"),
        safe_area_note="Keep units, dates, and source labels visible in both aspect ratios.",
    ),
    "map_geopolitical": CraftRules(
        narrative_structure="location → development → route or boundary → implication",
        shot_mix=("wide-map", "medium-map", "close-label", "evidence"),
        cut_rhythm="measured",
        transition_guidance=(
            "Slide between map scales; cut between a map and verified location imagery."
        ),
        color_grade="cool",
        exposure=1.0,
        contrast=1.08,
        saturation=0.95,
        temperature=-0.1,
        music_gain=0.07,
        ducking=True,
        sfx_gain=0.02,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "geopolitical explainer"),
        safe_area_note="Keep locator labels inside safe margins and mark disputed areas clearly.",
    ),
    "social": CraftRules(
        narrative_structure="hook → payoff → context → source/CTA",
        shot_mix=("close", "medium", "wide"),
        cut_rhythm="quick",
        transition_guidance=(
            "Use hard cuts on the beat; reserve a slide for a clear section change."
        ),
        color_grade="high-contrast-warm",
        exposure=1.04,
        contrast=1.12,
        saturation=1.1,
        temperature=0.06,
        music_gain=0.12,
        ducking=True,
        sfx_gain=0.03,
        caption_priority="high",
        platform_fit=("YouTube Shorts 9:16", "Reels 9:16", "TikTok 9:16"),
        safe_area_note="Keep captions above the bottom 18% and clear of right-side platform UI.",
    ),
    "minimal": CraftRules(
        narrative_structure="hook → development → resolution",
        shot_mix=("wide", "medium", "close"),
        cut_rhythm="lingering",
        transition_guidance="Use cuts and soft fades only; no decorative wipes.",
        color_grade="neutral",
        exposure=1.0,
        contrast=1.02,
        saturation=0.9,
        temperature=0.0,
        music_gain=0.0,
        ducking=False,
        sfx_gain=0.0,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "web"),
        safe_area_note="Generous negative space; keep text inside a 10% margin.",
    ),
    "photojournalism": CraftRules(
        narrative_structure="hook → photographic evidence → context → resolution",
        shot_mix=("wide", "medium", "close"),
        cut_rhythm="measured",
        transition_guidance=(
            "Cut between photographs; dissolve only when moving between time periods."
        ),
        color_grade="neutral-warm",
        exposure=1.01,
        contrast=1.06,
        saturation=1.0,
        temperature=0.05,
        music_gain=0.06,
        ducking=True,
        sfx_gain=0.02,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "photo essay"),
        safe_area_note="Never crop a subject's face; identify illustrative imagery explicitly.",
    ),
    "split_screen": CraftRules(
        narrative_structure="question → comparison → evidence → implication",
        shot_mix=("split", "medium", "close"),
        cut_rhythm="beat",
        transition_guidance=(
            "Wipe between equivalent panels; use hard cuts when the comparison changes."
        ),
        color_grade="neutral",
        exposure=1.0,
        contrast=1.08,
        saturation=0.95,
        temperature=0.0,
        music_gain=0.07,
        ducking=True,
        sfx_gain=0.01,
        caption_priority="high",
        platform_fit=("YouTube 16:9", "comparison explainer"),
        safe_area_note="Keep both panels' labels and units aligned and inside safe margins.",
    ),
    "timeline": CraftRules(
        narrative_structure="origin → development → present → open questions",
        shot_mix=("wide", "medium", "document", "close"),
        cut_rhythm="lingering",
        transition_guidance=(
            "Dissolve between archival periods; cut on the date when the record is confirmed."
        ),
        color_grade="warm-archival",
        exposure=1.0,
        contrast=1.1,
        saturation=0.85,
        temperature=0.15,
        music_gain=0.06,
        ducking=True,
        sfx_gain=0.01,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "historical explainer"),
        safe_area_note="Date labels remain readable and archival provenance stays on screen.",
    ),
    "kinetic_type": CraftRules(
        narrative_structure="word hit → payoff → context → source",
        shot_mix=("close", "typography"),
        cut_rhythm="beat",
        transition_guidance=(
            "Cut on the beat; use one radial reveal only for the central statistic."
        ),
        color_grade="high-contrast",
        exposure=1.05,
        contrast=1.15,
        saturation=1.05,
        temperature=0.0,
        music_gain=0.12,
        ducking=True,
        sfx_gain=0.02,
        caption_priority="high",
        platform_fit=("YouTube Shorts 9:16", "Reels 9:16", "TikTok 9:16"),
        safe_area_note="One idea per card; never animate words faster than they can be read.",
    ),
    "breaking": CraftRules(
        narrative_structure="verified update → context → what is known → what next",
        shot_mix=("wide", "medium", "close"),
        cut_rhythm="quick",
        transition_guidance="Hard cuts only; a timestamp band carries the update.",
        color_grade="neutral-high-contrast",
        exposure=1.02,
        contrast=1.1,
        saturation=1.0,
        temperature=0.0,
        music_gain=0.0,
        ducking=False,
        sfx_gain=0.0,
        caption_priority="high",
        platform_fit=("YouTube 16:9", "live update", "social"),
        safe_area_note=(
            "Timestamp and location stay visible; never label unverified news as breaking."
        ),
    ),
    "interview": CraftRules(
        narrative_structure="speaker → statement → context → evidence",
        shot_mix=("medium", "close", "wide", "document"),
        cut_rhythm="measured",
        transition_guidance=(
            "Use cuts between speaker and evidence; dissolve only for a change of location."
        ),
        color_grade="neutral",
        exposure=1.0,
        contrast=1.05,
        saturation=0.95,
        temperature=0.03,
        music_gain=0.05,
        ducking=True,
        sfx_gain=0.0,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "interview"),
        safe_area_note="Speaker name and direct-quote marks remain on screen long enough to read.",
    ),
    "diagram": CraftRules(
        narrative_structure="system → components → process → implication",
        shot_mix=("wide-diagram", "medium-diagram", "close-label"),
        cut_rhythm="beat",
        transition_guidance=(
            "Use a circular reveal only when moving between system levels; otherwise cut."
        ),
        color_grade="cool",
        exposure=1.02,
        contrast=1.06,
        saturation=0.9,
        temperature=-0.05,
        music_gain=0.07,
        ducking=True,
        sfx_gain=0.02,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "explainer"),
        safe_area_note="Labels stay attached to their components in both aspect ratios.",
    ),
    "archival": CraftRules(
        narrative_structure="archive → context → present → provenance",
        shot_mix=("wide", "medium", "document", "close"),
        cut_rhythm="lingering",
        transition_guidance=(
            "Dissolve between archival periods; cut when returning to the present."
        ),
        color_grade="sepia-warm",
        exposure=0.97,
        contrast=1.12,
        saturation=0.75,
        temperature=0.2,
        music_gain=0.0,
        ducking=False,
        sfx_gain=0.0,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "archive"),
        safe_area_note="Always state the original date; never imply archival footage is current.",
    ),
    "magazine": CraftRules(
        narrative_structure="presenter hook → story → context → close",
        shot_mix=("wide", "medium", "close", "interview"),
        cut_rhythm="measured",
        transition_guidance=(
            "Use smooth transitions between segments and hard cuts inside an interview."
        ),
        color_grade="warm",
        exposure=1.02,
        contrast=1.06,
        saturation=1.0,
        temperature=0.08,
        music_gain=0.08,
        ducking=True,
        sfx_gain=0.02,
        caption_priority="medium",
        platform_fit=("YouTube 16:9", "weekly roundup"),
        safe_area_note="Presenter graphics and lower-thirds share one consistent position.",
    ),
    "audio_led": CraftRules(
        narrative_structure="hook → development → resolution",
        shot_mix=("wide", "medium", "close"),
        cut_rhythm="lingering",
        transition_guidance=(
            "Change visuals on sentence boundaries; use soft fades rather than arbitrary cuts."
        ),
        color_grade="neutral",
        exposure=1.0,
        contrast=1.03,
        saturation=0.9,
        temperature=0.0,
        music_gain=0.0,
        ducking=False,
        sfx_gain=0.0,
        caption_priority="high",
        platform_fit=("YouTube 16:9", "audio-led"),
        safe_area_note="Captions follow the spoken sentence, not an arbitrary cut grid.",
    ),
}

for _key, _preset in list(PRESETS.items()):
    PRESETS[_key] = replace(_preset, craft=CRAFT_RULES[_key])


def get_preset(key: str | None) -> EditingPreset:
    return PRESETS.get(key or DEFAULT_PRESET_KEY, PRESETS[DEFAULT_PRESET_KEY])


def list_presets() -> list[dict[str, Any]]:
    return [preset.to_public_dict() for preset in PRESETS.values()]
