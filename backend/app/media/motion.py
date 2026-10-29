"""Deterministic, reusable motion-graphics engine.

The engine is deliberately renderer-agnostic at its core: a scene is analysed
into serialisable :class:`GraphicSpec` objects, those specs are evaluated at a
timestamp with keyframes/easing, and the result is rasterised into transparent
PNG frames. FFmpeg composites the frames over the finished scene video. News
and non-news productions therefore share the same animation primitives and
only differ in their content-aware planning and style profile.

No browser, GPU, or real-time animation clock is involved, so a frame rendered
at 2.4 seconds is always identical regardless of rendering order.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from backend.app.media.graphics import (
    _font,
    _rgba,
    _text_size,
    extract_figures,
    extract_percentages,
    extract_years,
    wrap_text,
)
from backend.app.media.presets import EditingPreset

# --------------------------------------------------------------------- easing


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def cubic_bezier(t: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Evaluate a CSS-style cubic Bézier easing curve at time ``t``."""

    t = _clamp(t)
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0

    def curve(u: float, p1: float, p2: float) -> float:
        return 3 * (1 - u) ** 2 * u * p1 + 3 * (1 - u) * u**2 * p2 + u**3

    low, high = 0.0, 1.0
    for _ in range(18):
        mid = (low + high) / 2
        if curve(mid, x1, x2) < t:
            low = mid
        else:
            high = mid
    return curve((low + high) / 2, y1, y2)


def ease(name: str, t: float) -> float:
    t = _clamp(t)
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    key = (name or "ease_in_out").strip().lower()
    if key in {"linear", "none"}:
        return t
    if key == "ease_in":
        return t * t
    if key == "ease_out":
        return 1 - (1 - t) ** 2
    if key == "ease_in_out":
        return 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2
    if key == "ease_out_cubic":
        return 1 - (1 - t) ** 3
    if key == "ease_in_cubic":
        return t**3
    if key == "overshoot":
        c1 = 1.70158
        c3 = c1 + 1
        return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2
    if key == "smooth":
        return cubic_bezier(t, 0.4, 0.0, 0.2, 1.0)
    return cubic_bezier(t, 0.42, 0.0, 0.58, 1.0)


@dataclass(frozen=True)
class Keyframe:
    """One property value at one local time (seconds)."""

    time: float
    value: float


def evaluate_keyframes(
    keyframes: Iterable[Keyframe],
    time_seconds: float,
    *,
    easing: str = "ease_in_out",
) -> float:
    """Interpolate a sorted keyframe list deterministically."""

    frames = sorted(keyframes, key=lambda frame: frame.time)
    if not frames:
        return 0.0
    if time_seconds <= frames[0].time:
        return frames[0].value
    if time_seconds >= frames[-1].time:
        return frames[-1].value
    for left, right in zip(frames, frames[1:], strict=False):
        if left.time <= time_seconds <= right.time:
            span = max(1e-6, right.time - left.time)
            progress = ease(easing, (time_seconds - left.time) / span)
            return left.value + (right.value - left.value) * progress
    return frames[-1].value


# ---------------------------------------------------------------- data model

GRAPHIC_TYPES = {
    "headline",
    "lower_third",
    "stat",
    "chart",
    "timeline",
    "map",
    "callout",
    "quote",
    "diagram",
    "comparison",
    "split",
    "source",
    "ticker",
    "progress",
}


@dataclass
class GraphicSpec:
    """A complete, serialisable description of one animated graphic."""

    id: str
    kind: str
    scene_position: int
    start_seconds: float
    duration_seconds: float
    template: str = ""
    layer: int = 20
    text: str = ""
    secondary_text: str = ""
    label: str = ""
    value: str = ""
    unit: str = ""
    source: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    x: float = 0.06
    y: float = 0.62
    width: float = 0.46
    height: float = 0.18
    align: str = "left"
    entry: str = "fade_slide"
    exit: str = "fade"
    text_animation: str = "fade"
    easing: str = "ease_out"
    anchor_x: float = 0.5
    anchor_y: float = 0.4
    safe_area: bool = True
    enabled: bool = True
    edited: bool = False
    style_key: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "scene_position": self.scene_position,
            "start_seconds": round(self.start_seconds, 3),
            "duration_seconds": round(self.duration_seconds, 3),
            "template": self.template or self.kind,
            "layer": self.layer,
            "text": self.text,
            "secondary_text": self.secondary_text,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "data": self.data,
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "width": round(self.width, 4),
            "height": round(self.height, 4),
            "align": self.align,
            "entry": self.entry,
            "exit": self.exit,
            "text_animation": self.text_animation,
            "easing": self.easing,
            "anchor_x": round(self.anchor_x, 4),
            "anchor_y": round(self.anchor_y, 4),
            "safe_area": self.safe_area,
            "enabled": self.enabled,
            "edited": self.edited,
            "style_key": self.style_key,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GraphicSpec:
        known = {field_name for field_name in cls.__dataclass_fields__}
        values = {key: value for key, value in payload.items() if key in known}
        if "data" in values and not isinstance(values["data"], list):
            values["data"] = []
        return cls(**values)


@dataclass(frozen=True)
class OverlaySequence:
    """A transparent PNG sequence and the absolute slot it occupies."""

    directory: Path
    start_seconds: float
    duration_seconds: float
    fps: int
    width: int
    height: int
    label: str = ""

    @property
    def pattern(self) -> Path:
        return self.directory / "frame-%05d.png"

    @property
    def frame_count(self) -> int:
        return max(1, round(self.duration_seconds * self.fps))


@dataclass
class SceneGraphicPlan:
    """The renderer consumes this plan directly; nothing here is advisory only."""

    scene_position: int
    purpose: str
    shot: str
    movement: str
    transition: str
    pacing: str
    rationale: str
    graphics: list[GraphicSpec] = field(default_factory=list)
    checks: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_position": self.scene_position,
            "purpose": self.purpose,
            "shot": self.shot,
            "movement": self.movement,
            "transition": self.transition,
            "pacing": self.pacing,
            "rationale": self.rationale,
            "graphics": [graphic.to_dict() for graphic in self.graphics],
            "checks": self.checks,
        }


# ------------------------------------------------------------- content tools

LOCATION_TERMS = {
    "border",
    "city",
    "coast",
    "country",
    "district",
    "island",
    "map",
    "province",
    "region",
    "route",
    "state",
    "town",
    "village",
    "world",
}
COMPARISON_TERMS = {
    "compared",
    "comparison",
    "versus",
    "vs",
    "before",
    "after",
    "higher",
    "lower",
    "more than",
    "less than",
}
PROCESS_TERMS = {
    "first",
    "second",
    "third",
    "then",
    "next",
    "step",
    "process",
    "how",
    "because",
    "leads to",
    "results in",
}
QUOTE_TERMS = {"said", "says", "told", "according to", "statement", "quote"}
PURPOSE_BY_KIND = {
    "headline": "establish",
    "lower_third": "identify",
    "stat": "explain",
    "chart": "compare",
    "timeline": "context",
    "map": "locate",
    "callout": "focus",
    "quote": "attribute",
    "diagram": "explain",
    "comparison": "compare",
    "split": "compare",
    "source": "attribute",
    "ticker": "context",
    "progress": "explain",
}

# Panel treatments carry their own frame furniture. A scene whose base frame
# already uses one should not get a second panel composited over it.
_PANEL_GRAPHIC_KINDS = {
    "map",
    "chart",
    "timeline",
    "diagram",
    "stat",
    "quote",
    "comparison",
    "split",
}


def _first_sentence(text: str, limit: int = 120) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    sentence = re.split(r"(?<=[.!?])\s", clean, maxsplit=1)[0]
    if len(sentence) <= limit:
        return sentence
    return sentence[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def _sentence_with_year(text: str, year: str) -> str:
    for sentence in re.split(r"(?<=[.!?])\s", text or ""):
        if year in sentence:
            return _first_sentence(sentence, 64)
    return year


def _has_any(text: str, terms: Iterable[str]) -> bool:
    lowered = " ".join((text or "").lower().split())
    return any(term in lowered for term in terms)


def _timing(start: float, duration: float, *, first: bool = False) -> tuple[float, float]:
    """Keep graphics inside their scene with a readable minimum duration."""

    duration = max(0.5, duration)
    lead = 0.25 if first else 0.4
    tail = 0.45
    available = max(1.0, duration - lead - tail)
    graphic_duration = min(max(2.2, duration * (0.62 if first else 0.55)), available)
    graphic_duration = max(1.0, graphic_duration)
    return start + min(lead, max(0.0, duration - graphic_duration)), graphic_duration


def _safe_box(kind: str, aspect_ratio: str, captions: bool) -> tuple[float, float, float, float]:
    portrait = aspect_ratio == "9:16"
    if kind == "headline":
        return (0.06, 0.08, 0.88, 0.24 if not portrait else 0.28)
    if kind in {"lower_third", "source", "ticker"}:
        y = 0.66 if not portrait else 0.58
        if captions:
            y = 0.58 if not portrait else 0.52
        return (0.06, y, 0.58 if not portrait else 0.88, 0.15 if not portrait else 0.17)
    if kind in {"chart", "stat", "timeline", "diagram", "comparison", "split"}:
        # Portrait headlines can run to four lines, so a portrait panel starts
        # below them instead of at the landscape height.
        return (0.07, 0.24, 0.86, 0.42 if not portrait else 0.34)
    if kind == "map":
        return (0.08, 0.20 if not portrait else 0.34, 0.84, 0.48 if not portrait else 0.34)
    if kind == "quote":
        return (0.08, 0.26, 0.84, 0.32)
    if kind == "callout":
        return (0.08, 0.30, 0.62, 0.16)
    if kind == "progress":
        return (0.08, 0.78, 0.84, 0.12)
    return (0.06, 0.62, 0.50, 0.18)


# --------------------------------------------------------------- planning


def build_editing_plan(
    *,
    scene_position: int,
    narration: str,
    duration_seconds: float,
    preset: EditingPreset,
    profile: Any,
    has_asset: bool,
    asset_kind: str,
) -> dict[str, Any]:
    """Describe purpose, shot, movement, transition, and pacing for one scene."""

    text = " ".join((narration or "").lower().split())
    if scene_position == 1:
        purpose = "establish"
    elif _has_any(text, COMPARISON_TERMS):
        purpose = "compare"
    elif _has_any(text, PROCESS_TERMS):
        purpose = "explain"
    elif _has_any(text, QUOTE_TERMS):
        purpose = "attribute"
    elif _has_any(text, LOCATION_TERMS):
        purpose = "locate"
    else:
        purpose = "develop"

    shot = preset.craft.shot_mix[(scene_position - 1) % max(1, len(preset.craft.shot_mix))]
    movement = "static" if not has_asset else preset.motion
    if asset_kind == "video":
        movement = "static"
    if purpose in {"compare", "explain", "locate"} and has_asset:
        movement = "slow_pan" if preset.motion == "static" else preset.motion
    transition = "fade" if scene_position == 1 else preset.transition
    pacing = (
        "quick" if duration_seconds < 4.5 else "measured" if duration_seconds < 8 else "lingering"
    )
    rationale = (
        f"{purpose.title()} scene: {shot} shot with {movement.replace('_', ' ')} movement; "
        f"{'licensed footage' if has_asset else 'designed visual'} carries the frame."
    )
    return {
        "purpose": purpose,
        "shot": shot,
        "movement": movement,
        "transition": transition,
        "pacing": pacing,
        "has_asset": has_asset,
        "asset_kind": asset_kind,
        "rationale": rationale,
    }


def _complement_frame(graphics: list[GraphicSpec], frame_type: str) -> list[GraphicSpec]:
    """Drop overlays that would repeat or cover the frame's own treatment.

    A frame that already carries a data panel, or a licensed photograph, should
    not get a second panel pasted over it: the overlay hides the evidence and
    makes the run look like the same card repeated. Those frames may still get
    identification or callout graphics, or nothing at all.
    """

    if frame_type in _PANEL_GRAPHIC_KINDS or frame_type == "photo":
        return [graphic for graphic in graphics if graphic.kind not in _PANEL_GRAPHIC_KINDS]
    return graphics


def _limit_panel_overlays(
    plans: list[SceneGraphicPlan],
    frame_types: dict[int, str],
) -> None:
    """Keep panel overlays from stacking up into a slideshow.

    The base frames already cap how many panels a run may use. This pass counts
    those frames against the same budget and refuses a panel next to another
    panel frame. A refused panel is dropped rather than shrunk: a small strip of
    map furniture reads as a rendering fault, not as an annotation.
    """

    ordered = sorted(plans, key=lambda plan: plan.scene_position)
    panel_frames = {
        plan.scene_position
        for plan in ordered
        if frame_types.get(plan.scene_position) in _PANEL_GRAPHIC_KINDS
    }
    budget = max(1, math.ceil(len(ordered) * 0.5))
    used = len(panel_frames)
    for index, plan in enumerate(ordered):
        previous_type = frame_types.get(ordered[index - 1].scene_position, "") if index else ""
        next_type = (
            frame_types.get(ordered[index + 1].scene_position, "")
            if index + 1 < len(ordered)
            else ""
        )
        adjacent_panel = previous_type in _PANEL_GRAPHIC_KINDS or next_type in _PANEL_GRAPHIC_KINDS
        kept: list[GraphicSpec] = []
        for graphic in plan.graphics:
            if graphic.kind not in _PANEL_GRAPHIC_KINDS:
                kept.append(graphic)
                continue
            if adjacent_panel or used >= budget:
                continue
            used += 1
            kept.append(graphic)
        plan.graphics = kept


def _base_spec(
    *,
    kind: str,
    scene_position: int,
    start: float,
    duration: float,
    aspect_ratio: str,
    captions: bool,
    style_key: str,
) -> GraphicSpec:
    x, y, width, height = _safe_box(kind, aspect_ratio, captions)
    return GraphicSpec(
        id=f"g-{scene_position:03d}-{kind}",
        kind=kind,
        scene_position=scene_position,
        start_seconds=start,
        duration_seconds=duration,
        template=kind,
        x=x,
        y=y,
        width=width,
        height=height,
        style_key=style_key,
    )


def plan_scene_graphics(
    *,
    scene_position: int,
    start_seconds: float,
    duration_seconds: float,
    narration: str,
    subtitle: str = "",
    keywords: list[str] | None = None,
    preset: EditingPreset,
    profile: Any,
    has_asset: bool = False,
    asset_kind: str = "photo",
    aspect_ratio: str = "16:9",
    captions_enabled: bool = True,
    frame_type: str = "",
) -> SceneGraphicPlan:
    """Choose meaningful graphics from the actual scene content.

    The planner is allowed to return no graphic. Footage, a cut, or stillness is
    often the better editorial choice, especially for breaking and sensitive
    stories. Every number, year, and label comes from the narration text.
    """

    text = " ".join((narration or subtitle or "").split())
    genre = profile.genre
    templates = set(genre.graphic_templates)
    density = float(genre.graphic_density)
    sensitive = genre.sensitivity in {"sensitive", "high"}
    editing = build_editing_plan(
        scene_position=scene_position,
        narration=text,
        duration_seconds=duration_seconds,
        preset=preset,
        profile=profile,
        has_asset=has_asset,
        asset_kind=asset_kind,
    )
    graphics: list[GraphicSpec] = []
    if density <= 0 or not templates:
        return SceneGraphicPlan(
            scene_position=scene_position,
            purpose=editing["purpose"],
            shot=editing["shot"],
            movement=editing["movement"],
            transition=editing["transition"],
            pacing=editing["pacing"],
            rationale=editing["rationale"] + " Motion graphics are disabled for this profile.",
            graphics=[],
            checks=[],
        )

    figures = extract_figures(text)
    percentages = extract_percentages(text)
    years = extract_years(text)
    location = _has_any(text, LOCATION_TERMS)
    comparison = _has_any(text, COMPARISON_TERMS)
    process = _has_any(text, PROCESS_TERMS)
    quote = _has_any(text, QUOTE_TERMS)
    start, length = _timing(start_seconds, duration_seconds, first=scene_position == 1)

    # 1. Opening title / headline: only when it adds orientation, never every scene.
    if scene_position == 1 and subtitle and "headline" in templates:
        spec = _base_spec(
            kind="headline",
            scene_position=scene_position,
            start=start_seconds + 0.15,
            duration=max(2.0, min(duration_seconds - 0.4, 3.8)),
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        spec.text = _first_sentence(subtitle or text, 96)
        spec.label = genre.name
        spec.entry = "fade_slide"
        spec.text_animation = "line"
        spec.source = ""
        graphics.append(spec)

    # 2. Data graphics are the highest-value treatment when real values exist.
    if "chart" in templates and len(percentages) >= 2 and density >= 0.45:
        spec = _base_spec(
            kind="chart",
            scene_position=scene_position,
            start=start,
            duration=length,
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        spec.text = "Figures stated in the narration"
        spec.data = [
            {"label": context[:34] or f"Value {index + 1}", "value": round(value, 2), "unit": "%"}
            for index, (context, value) in enumerate(percentages[:5])
        ]
        spec.source = "As reported"
        spec.entry = "mask_reveal"
        spec.text_animation = "line"
        spec.meta = {"chart": "bar", "show_axis": True}
        graphics.append(spec)
    elif "stat" in templates and figures and density >= 0.4:
        context, value = figures[0]
        spec = _base_spec(
            kind="stat",
            scene_position=scene_position,
            start=start,
            duration=length,
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        spec.text = context[:60] or "Reported figure"
        spec.value = value
        spec.source = "As reported"
        spec.entry = "fade_scale"
        spec.text_animation = "count" if re.fullmatch(r"[\d.,%$€£]+", value) else "fade"
        graphics.append(spec)

    # 3. A timeline is only useful with at least two real dates.
    if "timeline" in templates and len(years) >= 2 and density >= 0.4 and len(graphics) < 2:
        spec = _base_spec(
            kind="timeline",
            scene_position=scene_position,
            start=start,
            duration=length,
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        spec.text = "Timeline stated in the narration"
        spec.data = [{"date": year, "label": _sentence_with_year(text, year)} for year in years[:5]]
        spec.source = "As reported"
        spec.entry = "line_draw"
        spec.text_animation = "line"
        graphics.append(spec)

    # 4. Locator map: illustrative, explicitly labelled, never invented borders.
    if "map" in templates and location and density >= 0.5 and len(graphics) < 2:
        spec = _base_spec(
            kind="map",
            scene_position=scene_position,
            start=start,
            duration=length,
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        spec.label = (keywords or [])[0] if keywords else "Location"
        spec.text = "Location context"
        spec.source = "Illustrative locator"
        spec.entry = "fade_scale"
        spec.text_animation = "fade"
        spec.meta = {"illustrative": True}
        graphics.append(spec)

    # 5. Comparison, process, quote, and callout treatments.
    if (
        len(graphics) < 2
        and comparison
        and len(figures) >= 2
        and "comparison" in templates
        and density >= 0.45
    ):
        spec = _base_spec(
            kind="comparison",
            scene_position=scene_position,
            start=start,
            duration=length,
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        values = figures[:2]
        spec.text = "Comparison stated in the narration"
        spec.data = [
            {"label": context[:30] or "Value", "value": value} for context, value in values
        ]
        spec.source = "As reported"
        spec.entry = "split_reveal"
        spec.text_animation = "line"
        graphics.append(spec)
    elif len(graphics) < 2 and process and "diagram" in templates and density >= 0.5:
        spec = _base_spec(
            kind="diagram",
            scene_position=scene_position,
            start=start,
            duration=length,
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        steps = _extract_steps(text)
        spec.text = "Process described in the narration"
        spec.data = [{"label": step} for step in steps[:4]]
        spec.source = "As described"
        spec.entry = "sequence"
        spec.text_animation = "word"
        graphics.append(spec)
    elif len(graphics) < 2 and quote and "quote" in templates and density >= 0.35:
        quoted = _quote_text(text)
        if quoted:
            spec = _base_spec(
                kind="quote",
                scene_position=scene_position,
                start=start,
                duration=length,
                aspect_ratio=aspect_ratio,
                captions=captions_enabled,
                style_key=preset.key,
            )
            spec.text = quoted
            spec.secondary_text = ""
            spec.source = "As reported"
            spec.entry = "fade_slide"
            spec.text_animation = "word"
            graphics.append(spec)
    elif (
        len(graphics) < 2
        and has_asset
        and "callout" in templates
        and scene_position % 3 == 0
        and density >= 0.5
    ):
        spec = _base_spec(
            kind="callout",
            scene_position=scene_position,
            start=start,
            duration=min(length, 4.0),
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        spec.text = (keywords or [])[0] if keywords else "Key detail"
        spec.anchor_x = 0.64
        spec.anchor_y = 0.38
        spec.entry = "line_draw"
        spec.text_animation = "fade"
        graphics.append(spec)

    # 6. Identification graphics are used sparingly, and never over sensitive faces.
    if (
        not graphics
        and "lower_third" in templates
        and has_asset
        and not sensitive
        and scene_position % 2 == 0
        and density >= 0.35
    ):
        spec = _base_spec(
            kind="lower_third",
            scene_position=scene_position,
            start=start,
            duration=min(length, 4.2),
            aspect_ratio=aspect_ratio,
            captions=captions_enabled,
            style_key=preset.key,
        )
        spec.text = (keywords or [genre.name])[0].title()
        spec.secondary_text = genre.name
        spec.source = ""
        spec.entry = "fade_slide"
        spec.text_animation = "line"
        graphics.append(spec)

    # 7. A designed typography card has no footage to carry it, and a photo of a
    #    named person benefits from identification. A lower third that names a
    #    real person or institution is honest and gives the frame its motion.
    if not graphics and frame_type in {"typography", "photo"}:
        spec = _identification_spec(
            scene_position=scene_position,
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
            narration=text,
            preset=preset,
            profile=profile,
            aspect_ratio=aspect_ratio,
            captions_enabled=captions_enabled,
        )
        if spec is not None:
            graphics.append(spec)

    graphics = _complement_frame(graphics, frame_type)

    for graphic in graphics:
        graphic.start_seconds = max(start_seconds, graphic.start_seconds)
        graphic.duration_seconds = min(
            graphic.duration_seconds,
            max(1.0, start_seconds + duration_seconds - graphic.start_seconds - 0.2),
        )

    checks = graphic_checks(
        graphics,
        aspect_ratio=aspect_ratio,
        captions_enabled=captions_enabled,
        scene_start=start_seconds,
        scene_end=start_seconds + duration_seconds,
    )
    return SceneGraphicPlan(
        scene_position=scene_position,
        purpose=editing["purpose"],
        shot=editing["shot"],
        movement=editing["movement"],
        transition=editing["transition"],
        pacing=editing["pacing"],
        rationale=editing["rationale"],
        graphics=graphics,
        checks=checks,
    )


def _extract_steps(text: str) -> list[str]:
    parts = re.split(
        r"(?:\bfirst\b|\bsecond\b|\bthird\b|\bthen\b|\bnext\b|\bfinally\b|;)", text, flags=re.I
    )
    steps = [_first_sentence(part.strip(" ,."), 52) for part in parts if part.strip(" ,.")]
    if len(steps) < 2:
        sentences = re.split(r"(?<=[.!?])\s", text)
        steps = [_first_sentence(sentence, 52) for sentence in sentences if sentence.strip()]
    return steps[:4]


_SENTENCE_STARTERS = frozenset(
    {
        "the",
        "this",
        "that",
        "these",
        "those",
        "more",
        "but",
        "and",
        "after",
        "before",
        "when",
        "while",
        "one",
        "two",
        "three",
        "it",
        "he",
        "she",
        "they",
        "we",
        "his",
        "her",
        "their",
        "our",
    }
)


def _proper_name(text: str) -> str:
    """Find a short proper name that can honestly carry an identification.

    Only two- or three-word capitalised phrases qualify, so a stray capitalised
    adjective cannot become a chyron. Possessives ("BBC's") are rejected because
    they read as a truncated headline rather than a name.
    """

    for match in re.finditer(r"\b([A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+)+)\b", text or ""):
        words = match.group(1).split()
        while words and words[0].lower() in _SENTENCE_STARTERS:
            words.pop(0)
        if not 2 <= len(words) <= 3:
            continue
        if words[0].endswith(("'s", "’s")):
            continue
        phrase = " ".join(words)
        if "-" in phrase:
            continue
        # A capitalised adjective phrase ("Russian-occupied Ukrainian") is not
        # a name; a chyron needs a noun at the end.
        if words[-1].lower().endswith(("ian", "ese", "ish", "ic")):
            continue
        if 6 <= len(phrase) <= 40:
            return phrase
    return ""


_ROLE_NOUNS = frozenset(
    {
        "correspondent",
        "reporter",
        "editor",
        "anchor",
        "minister",
        "president",
        "prime",
        "spokesperson",
        "spokesman",
        "spokeswoman",
        "mayor",
        "governor",
        "official",
        "analyst",
        "professor",
        "director",
        "secretary",
        "envoy",
        "ambassador",
        "chief",
        "commander",
        "leader",
        "judge",
    }
)


def _role_for_name(name: str, text: str) -> str:
    """Return a short role that the narration actually attaches to the name.

    A chyron's second line is a job title, not the story's genre. Only words
    the narration places immediately before the name are used, so nothing is
    invented. "BBC's Ukraine correspondent, Dan Johnson" yields "Ukraine
    correspondent"; "the White House" yields no role.
    """

    cleaned = " ".join((text or "").split())
    if not cleaned or not name:
        return ""
    index = cleaned.find(name)
    if index <= 0:
        return ""
    before = cleaned[:index].rstrip(" ,;:-–—")
    words = [word.strip(".,;:()") for word in before.split()][-4:]
    lowered = [word.lower() for word in words]
    position = next(
        (i for i in range(len(lowered) - 1, -1, -1) if lowered[i] in _ROLE_NOUNS),
        None,
    )
    if position is None:
        return ""
    role_words = [
        word
        for word in words[max(0, position - 2) : position + 1]
        if word and not word.lower().endswith(("'s", "’s"))
    ]
    while role_words and role_words[0].lower() in {"the", "a", "an"}:
        role_words.pop(0)
    if not role_words:
        return ""
    role = " ".join(role_words)
    return role[0].upper() + role[1:48]


def _identification_spec(
    *,
    scene_position: int,
    start_seconds: float,
    duration_seconds: float,
    narration: str,
    preset: EditingPreset,
    profile: Any,
    aspect_ratio: str,
    captions_enabled: bool,
) -> GraphicSpec | None:
    """Build a lower third only when a real name exists in the narration."""

    genre = profile.genre
    if genre.sensitivity in {"sensitive", "high"}:
        return None
    if "lower_third" not in set(genre.graphic_templates):
        return None
    if float(genre.graphic_density) < 0.35:
        return None
    subject = _proper_name(" ".join((narration or "").split()))
    if not subject:
        return None
    start, length = _timing(start_seconds, duration_seconds, first=scene_position == 1)
    spec = _base_spec(
        kind="lower_third",
        scene_position=scene_position,
        start=start,
        duration=min(length, 4.2),
        aspect_ratio=aspect_ratio,
        captions=captions_enabled,
        style_key=preset.key,
    )
    spec.text = subject
    # The second line is the role the narration actually attaches to the name.
    # Falling back to the genre name would put "World & Geopolitics" under a
    # person's name, which is meaningless to a viewer.
    spec.secondary_text = _role_for_name(subject, narration)
    spec.source = ""
    spec.entry = "fade_slide"
    spec.text_animation = "line"
    return spec


def _quote_text(text: str) -> str:
    match = re.search(r"[\"“](.+?)[\"”]", text)
    if match:
        return _first_sentence(match.group(1), 140)
    # Reported speech without quotation marks is not a quotation. Printing it
    # in quote styling would invent a direct quote the source never gave.
    return ""


# ------------------------------------------------------------------- checks


def graphic_checks(
    graphics: list[GraphicSpec],
    *,
    aspect_ratio: str,
    captions_enabled: bool,
    scene_start: float,
    scene_end: float,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if len(graphics) > 3:
        checks.append(
            {
                "name": "graphic_density",
                "status": "warning",
                "detail": f"{len(graphics)} graphics in one scene; keep the frame readable.",
            }
        )
    caption_top = 0.78 if aspect_ratio == "16:9" else 0.72
    for graphic in graphics:
        prefix = graphic.id
        if graphic.start_seconds < scene_start - 0.01 or (
            graphic.start_seconds + graphic.duration_seconds > scene_end + 0.05
        ):
            checks.append(
                {
                    "name": f"{prefix}_timing",
                    "status": "failed",
                    "detail": "Graphic extends outside its scene boundary.",
                }
            )
        if graphic.duration_seconds < 1.0:
            checks.append(
                {
                    "name": f"{prefix}_duration",
                    "status": "warning",
                    "detail": "Graphic is shorter than one second and may not be readable.",
                }
            )
        if (
            graphic.x < 0.02
            or graphic.y < 0.02
            or graphic.x + graphic.width > 0.98
            or graphic.y + graphic.height > 0.98
        ):
            checks.append(
                {
                    "name": f"{prefix}_safe_area",
                    "status": "failed",
                    "detail": "Graphic extends beyond the frame safe area.",
                }
            )
        if (
            captions_enabled
            and graphic.y + graphic.height > caption_top
            and graphic.kind
            not in {
                "headline",
                "progress",
            }
        ):
            checks.append(
                {
                    "name": f"{prefix}_caption_overlap",
                    "status": "warning",
                    "detail": "Graphic may overlap the reserved caption area.",
                }
            )
        if graphic.kind in {"chart", "stat", "timeline", "comparison"} and not graphic.source:
            checks.append(
                {
                    "name": f"{prefix}_source",
                    "status": "warning",
                    "detail": "Data graphic has no source attribution.",
                }
            )
        if graphic.kind in {"chart", "comparison"}:
            if not graphic.data:
                checks.append(
                    {
                        "name": f"{prefix}_data",
                        "status": "failed",
                        "detail": "Chart has no data.",
                    }
                )
            for item in graphic.data:
                value = item.get("value")
                if value not in (None, "") and not isinstance(value, (int, float)):
                    checks.append(
                        {
                            "name": f"{prefix}_data_value",
                            "status": "failed",
                            "detail": "Chart value is not numeric.",
                        }
                    )
    return checks


# ------------------------------------------------------------------ drawing


def _alpha(image: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 0.999:
        return image
    result = image.copy()
    channel = result.getchannel("A").point(lambda value: int(value * _clamp(alpha)))
    result.putalpha(channel)
    return result


def _draw_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font: Any,
    fill: tuple[int, int, int, int],
    max_width: int,
    *,
    max_lines: int = 4,
    reveal: float = 1.0,
    animation: str = "fade",
    line_gap: float = 1.18,
) -> int:
    text = " ".join((text or "").split())
    if not text:
        return xy[1]
    if animation in {"word", "typewriter", "char"} and reveal < 0.999:
        if animation == "char":
            visible = text[: max(1, round(len(text) * reveal))]
        else:
            words = text.split()
            visible = " ".join(words[: max(1, round(len(words) * reveal))])
        text = visible
    lines = wrap_text(draw, text, font, max_width, max_lines=max_lines)
    x, y = xy
    _, height = _text_size(draw, "Ag", font)
    step = max(1, int(height * line_gap))
    for line in lines:
        draw.text(
            (x, y), line, font=font, fill=fill, stroke_width=1, stroke_fill=(0, 0, 0, fill[3])
        )
        y += step
    return y


def _panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: str,
    alpha: int,
    radius: int = 14,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=_rgba(color, alpha))


def _draw_graphic(
    spec: GraphicSpec,
    preset: EditingPreset,
    width: int,
    height: int,
    local_t: float,
) -> Image.Image:
    """Draw one graphic into a full-frame transparent layer."""

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not spec.enabled:
        return canvas
    duration = max(0.1, spec.duration_seconds)
    entry_duration = min(0.55, max(0.18, duration * 0.22))
    exit_duration = min(0.45, max(0.15, duration * 0.18))
    entry_p = ease(spec.easing, _clamp(local_t / entry_duration))
    exit_p = ease("ease_in", _clamp((duration - local_t) / exit_duration))
    alpha = _clamp(min(entry_p if spec.entry != "fade" else entry_p, exit_p))
    reveal = _clamp(local_t / max(0.25, duration * 0.55))

    box_x = int(spec.x * width)
    box_y = int(spec.y * height)
    box_w = max(120, int(spec.width * width))
    box_h = max(64, int(spec.height * height))

    # Slide/scale entry transforms are applied to the rendered local layer.
    offset_x = 0
    offset_y = 0
    if spec.entry in {"fade_slide", "slide"}:
        offset_y = int((1 - entry_p) * height * 0.045)
    if spec.entry == "slide_left":
        offset_x = int((1 - entry_p) * width * 0.04)
    scale = 1.0
    if spec.entry == "fade_scale":
        scale = 0.94 + 0.06 * entry_p
    if spec.entry == "punch":
        scale = 1.0 + 0.035 * math.sin(entry_p * math.pi)

    local = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(local)
    margin = max(12, int(box_h * 0.12))
    accent = _rgba(preset.palette.accent, int(255 * alpha))
    text_color = _rgba(preset.palette.text, int(255 * alpha))
    muted = _rgba(preset.palette.muted, int(255 * alpha))
    surface_alpha = int(238 * alpha)
    kind = spec.kind

    if kind == "headline":
        _panel(
            draw,
            (0, 0, box_w, box_h),
            preset.palette.surface,
            surface_alpha,
            radius=max(8, box_h // 8),
        )
        draw.rectangle((0, 0, max(4, box_w // 90), box_h), fill=accent)
        kicker_size = max(12, int(box_h * 0.15))
        kicker_font = _font(preset.body_font, kicker_size)
        draw.text(
            (margin, margin), (spec.label or "NEWS").upper()[:36], font=kicker_font, fill=accent
        )
        font = _font(preset.heading_font, max(18, int(box_h * 0.28)))
        _draw_text(
            draw,
            spec.text,
            (margin, int(box_h * 0.34)),
            font,
            text_color,
            box_w - margin * 2,
            max_lines=2,
            reveal=reveal,
            animation=spec.text_animation,
            line_gap=1.08,
        )
    elif kind in {"lower_third", "source", "ticker"}:
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha, radius=8)
        draw.rectangle((0, 0, max(5, box_w // 70), box_h), fill=accent)
        title_size = max(16, int(box_h * 0.30))
        title_font = _font(preset.heading_font, title_size)
        _draw_text(
            draw,
            spec.text or spec.label,
            (margin, int(box_h * 0.16)),
            title_font,
            text_color,
            box_w - margin * 2,
            max_lines=1,
            reveal=reveal,
            animation=spec.text_animation,
        )
        small_font = _font(preset.body_font, max(11, int(box_h * 0.18)))
        secondary = spec.secondary_text or spec.source
        if secondary:
            draw.text(
                (margin, int(box_h * 0.63)),
                secondary[:64],
                font=small_font,
                fill=muted,
            )
        if spec.source and spec.source != secondary:
            draw.text((margin, int(box_h * 0.80)), spec.source[:72], font=small_font, fill=muted)
    elif kind == "stat":
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha)
        draw.rectangle((0, 0, max(5, box_w // 70), box_h), fill=accent)
        value_font = _font(preset.heading_font, max(28, int(box_h * 0.42)))
        value = spec.value
        if spec.text_animation == "count" and local_t < duration * 0.7:
            value = _count_value(spec.value, reveal)
        draw.text((margin, int(box_h * 0.14)), value, font=value_font, fill=text_color)
        label_font = _font(preset.body_font, max(12, int(box_h * 0.17)))
        _draw_text(
            draw,
            spec.text,
            (margin, int(box_h * 0.62)),
            label_font,
            muted,
            box_w - margin * 2,
            max_lines=2,
            reveal=1.0,
        )
        if spec.source:
            draw.text(
                (margin, int(box_h * 0.86)),
                spec.source[:70],
                font=_font(preset.body_font, max(9, int(box_h * 0.12))),
                fill=muted,
            )
    elif kind == "chart":
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha)
        title_font = _font(preset.body_font, max(11, int(box_h * 0.09)))
        draw.text((margin, margin), spec.text.upper()[:64], font=title_font, fill=accent)
        chart_top = margin + int(box_h * 0.16)
        chart_bottom = box_h - int(box_h * 0.16)
        data = spec.data[:5]
        if spec.meta.get("chart") == "donut" and data:
            _draw_donut(
                draw,
                data,
                (box_w // 2, (chart_top + chart_bottom) // 2),
                int(min(box_w, chart_bottom - chart_top) * 0.28),
                reveal,
                preset,
                alpha,
            )
        elif spec.meta.get("chart") == "line" and len(data) >= 2:
            _draw_line_chart(
                draw,
                data,
                (margin * 2, chart_top, box_w - margin * 2, chart_bottom),
                reveal,
                preset,
                alpha,
            )
        else:
            _draw_bar_chart(
                draw,
                data,
                (margin * 2, chart_top, box_w - margin * 2, chart_bottom),
                reveal,
                preset,
                alpha,
            )
        if spec.source:
            draw.text(
                (margin, box_h - int(box_h * 0.11)),
                spec.source[:72],
                font=_font(preset.body_font, max(9, int(box_h * 0.075))),
                fill=muted,
            )
    elif kind == "timeline":
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha)
        title_font = _font(preset.body_font, max(11, int(box_h * 0.09)))
        draw.text((margin, margin), spec.text.upper()[:64], font=title_font, fill=accent)
        _draw_timeline(
            draw,
            spec.data[:5],
            (margin * 2, int(box_h * 0.48), box_w - margin * 2),
            reveal,
            preset,
            alpha,
        )
        if spec.source:
            draw.text(
                (margin, box_h - int(box_h * 0.11)),
                spec.source[:72],
                font=_font(preset.body_font, max(9, int(box_h * 0.075))),
                fill=muted,
            )
    elif kind == "map":
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha)
        _draw_map(
            draw, spec, (margin, margin, box_w - margin, box_h - margin), reveal, preset, alpha
        )
    elif kind == "callout":
        _draw_callout(draw, spec, (box_w, box_h), reveal, preset, alpha)
    elif kind == "quote":
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha)
        draw.rectangle((0, 0, max(5, box_w // 70), box_h), fill=accent)
        quote_font = _font(preset.heading_font, max(16, int(box_h * 0.20)))
        _draw_text(
            draw,
            f"“{spec.text}”",
            (margin, int(box_h * 0.16)),
            quote_font,
            text_color,
            box_w - margin * 2,
            max_lines=3,
            reveal=reveal,
            animation=spec.text_animation,
        )
        if spec.source:
            draw.text(
                (margin, box_h - int(box_h * 0.12)),
                spec.source[:72],
                font=_font(preset.body_font, max(10, int(box_h * 0.09))),
                fill=muted,
            )
    elif kind == "diagram":
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha)
        draw.text(
            (margin, margin),
            spec.text.upper()[:64],
            font=_font(preset.body_font, max(11, int(box_h * 0.09))),
            fill=accent,
        )
        _draw_diagram(
            draw,
            spec.data[:4],
            (margin, int(box_h * 0.22), box_w - margin, box_h - margin),
            reveal,
            preset,
            alpha,
        )
        if spec.source:
            draw.text(
                (margin, box_h - int(box_h * 0.10)),
                spec.source[:72],
                font=_font(preset.body_font, max(9, int(box_h * 0.07))),
                fill=muted,
            )
    elif kind in {"comparison", "split"}:
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha)
        draw.text(
            (margin, margin),
            spec.text.upper()[:64],
            font=_font(preset.body_font, max(11, int(box_h * 0.09))),
            fill=accent,
        )
        _draw_comparison(
            draw,
            spec.data[:2],
            (margin, int(box_h * 0.22), box_w - margin, box_h - margin),
            reveal,
            preset,
            alpha,
        )
        if spec.source:
            draw.text(
                (margin, box_h - int(box_h * 0.10)),
                spec.source[:72],
                font=_font(preset.body_font, max(9, int(box_h * 0.07))),
                fill=muted,
            )
    elif kind == "progress":
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha, radius=box_h // 2)
        steps = spec.data or [{"label": str(index + 1)} for index in range(4)]
        _draw_progress(
            draw, steps[:5], (margin, box_h // 2, box_w - margin, box_h // 2), reveal, preset, alpha
        )
    else:
        _panel(draw, (0, 0, box_w, box_h), preset.palette.surface, surface_alpha)
        _draw_text(
            draw,
            spec.text,
            (margin, margin),
            _font(preset.body_font, max(12, int(box_h * 0.18))),
            text_color,
            box_w - margin * 2,
        )

    if scale != 1.0:
        new_size = (max(1, int(box_w * scale)), max(1, int(box_h * scale)))
        local = local.resize(new_size, Image.Resampling.LANCZOS)
        box_x -= (new_size[0] - box_w) // 2
        box_y -= (new_size[1] - box_h) // 2
    local = _alpha(local, alpha)
    canvas.alpha_composite(
        local,
        (
            max(0, min(width - local.width, box_x + offset_x)),
            max(0, min(height - local.height, box_y + offset_y)),
        ),
    )
    return canvas


def _count_value(value: str, progress: float) -> str:
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value or "")
    if not match:
        return value
    raw = match.group(0)
    try:
        number = float(raw.replace(",", ""))
    except ValueError:
        return value
    current = number * _clamp(progress)
    if "." in raw:
        rendered = f"{current:,.{len(raw.split('.')[1])}f}"
    else:
        rendered = f"{current:,.0f}"
    return value.replace(raw, rendered)


def _draw_bar_chart(draw, data, box, reveal, preset, alpha):
    x0, y0, x1, y1 = box
    if not data:
        return
    values = [float(item.get("value") or 0) for item in data]
    maximum = max(values + [1.0])
    row_h = (y1 - y0) / max(1, len(data))
    label_font = _font(preset.body_font, max(10, int(row_h * 0.24)))
    value_font = _font(preset.body_font, max(10, int(row_h * 0.24)))
    for index, item in enumerate(data):
        y = y0 + int(row_h * index)
        bar_y = y + int(row_h * 0.42)
        bar_h = max(6, int(row_h * 0.26))
        width = int(
            (x1 - x0) * _clamp(float(item.get("value") or 0) / maximum) * _clamp(reveal * 1.6)
        )
        draw.text(
            (x0, y),
            str(item.get("label") or "")[:34],
            font=label_font,
            fill=_rgba(preset.palette.text, int(255 * alpha)),
        )
        draw.rounded_rectangle(
            (x0, bar_y, x0 + max(2, width), bar_y + bar_h),
            radius=3,
            fill=_rgba(
                preset.palette.accent if index % 2 == 0 else preset.palette.secondary,
                int(255 * alpha),
            ),
        )
        draw.text(
            (x0 + max(2, width) + 6, bar_y - 1),
            f"{item.get('value')}{item.get('unit', '')}",
            font=value_font,
            fill=_rgba(preset.palette.muted, int(255 * alpha)),
        )


def _draw_line_chart(draw, data, box, reveal, preset, alpha):
    x0, y0, x1, y1 = box
    values = [float(item.get("value") or 0) for item in data]
    maximum = max(values + [1.0])
    minimum = min(values + [0.0])
    span = max(1e-6, maximum - minimum)
    points = []
    for index, item in enumerate(data):
        px = x0 + (x1 - x0) * index / max(1, len(data) - 1)
        py = y1 - (y1 - y0) * ((float(item.get("value") or 0) - minimum) / span)
        points.append((px, py))
    visible = max(1, int(len(points) * _clamp(reveal * 1.3)))
    for left, right in zip(points[:visible], points[1:visible], strict=False):
        draw.line(
            (left[0], left[1], right[0], right[1]),
            fill=_rgba(preset.palette.accent, int(255 * alpha)),
            width=max(2, int((y1 - y0) * 0.012)),
        )
    for _index, (point, item) in enumerate(zip(points[:visible], data[:visible], strict=False)):
        draw.ellipse(
            (point[0] - 5, point[1] - 5, point[0] + 5, point[1] + 5),
            fill=_rgba(preset.palette.accent, int(255 * alpha)),
        )
        draw.text(
            (point[0] - 10, y1 + 4),
            str(item.get("label") or "")[:12],
            font=_font(preset.body_font, 10),
            fill=_rgba(preset.palette.muted, int(255 * alpha)),
        )


def _draw_donut(draw, data, center, radius, reveal, preset, alpha):
    total = sum(max(0.0, float(item.get("value") or 0)) for item in data) or 1.0
    start = -90
    palette = [
        preset.palette.accent,
        preset.palette.secondary,
        preset.palette.muted,
        preset.palette.text,
    ]
    for index, item in enumerate(data):
        sweep = 360 * max(0.0, float(item.get("value") or 0)) / total * _clamp(reveal * 1.2)
        draw.arc(
            (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
            start=start,
            end=start + sweep,
            fill=_rgba(palette[index % len(palette)], int(255 * alpha)),
            width=max(8, radius // 3),
        )
        start += sweep


def _draw_timeline(draw, data, box, reveal, preset, alpha):
    x0, y0, x1 = box
    if not data:
        return
    draw.line(
        (x0, y0, x0 + int((x1 - x0) * _clamp(reveal * 1.4)), y0),
        fill=_rgba(preset.palette.accent, int(255 * alpha)),
        width=max(2, int((y0 - box[1]) * 0.03) or 2),
    )
    count = max(1, len(data))
    label_font = _font(preset.body_font, max(10, int((box[1] - box[0]) * 0.10) or 10))
    for index, item in enumerate(data):
        px = x0 + int((x1 - x0) * index / max(1, count - 1))
        visible = reveal >= (index + 1) / (count + 0.5)
        draw.ellipse(
            (px - 6, y0 - 6, px + 6, y0 + 6),
            fill=_rgba(
                preset.palette.accent if visible else preset.palette.muted, int(255 * alpha)
            ),
        )
        draw.text(
            (px - 18, y0 - 34),
            str(item.get("date") or "")[:12],
            font=label_font,
            fill=_rgba(preset.palette.text, int(255 * alpha)),
        )
        if visible:
            label = str(item.get("label") or "")[:34]
            draw.text(
                (px - 18, y0 + 12),
                label,
                font=label_font,
                fill=_rgba(preset.palette.muted, int(255 * alpha)),
            )


def _draw_map(draw, spec, box, reveal, preset, alpha):
    x0, y0, x1, y1 = box
    grid = _rgba(preset.palette.muted, int(65 * alpha))
    for index in range(1, 7):
        x = x0 + (x1 - x0) * index / 7
        draw.line((x, y0, x, y1), fill=grid, width=1)
    for index in range(1, 5):
        y = y0 + (y1 - y0) * index / 5
        draw.line((x0, y, x1, y), fill=grid, width=1)
    cx = x0 + int((x1 - x0) * 0.54)
    cy = y0 + int((y1 - y0) * 0.50)
    radius = int(min(x1 - x0, y1 - y0) * 0.22)
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=_rgba(preset.palette.secondary, int(150 * alpha)),
        width=max(2, radius // 18),
    )
    draw.arc(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        start=200,
        end=340,
        fill=_rgba(preset.palette.accent, int(160 * alpha)),
        width=max(2, radius // 18),
    )
    marker = _clamp(reveal * 1.4)
    draw.ellipse(
        (cx - 10, cy - 10, cx + 10, cy + 10), fill=_rgba(preset.palette.accent, int(255 * alpha))
    )
    draw.ellipse(
        (cx - 22, cy - 22, cx + 22, cy + 22),
        outline=_rgba(preset.palette.accent, int(160 * alpha)),
        width=2,
    )
    label_font = _font(preset.heading_font, max(14, int((y1 - y0) * 0.07)))
    draw.text(
        (x0, y1 - int((y1 - y0) * 0.22)),
        str(spec.label or "Location")[:28].upper(),
        font=label_font,
        fill=_rgba(preset.palette.text, int(255 * alpha)),
    )
    note_font = _font(preset.body_font, max(9, int((y1 - y0) * 0.045)))
    draw.text(
        (x0, y1 - int((y1 - y0) * 0.09)),
        spec.source[:74],
        font=note_font,
        fill=_rgba(preset.palette.muted, int(255 * alpha)),
    )
    if marker > 0.5:
        draw.text(
            (x0, y0),
            "LOCATION CONTEXT",
            font=_font(preset.body_font, max(10, int((y1 - y0) * 0.055))),
            fill=_rgba(preset.palette.accent, int(255 * alpha)),
        )


def _draw_callout(draw, spec, box, reveal, preset, alpha):
    width, height = box
    anchor_x = int(spec.anchor_x * width)
    anchor_y = int(spec.anchor_y * height)
    target_x = int(width * 0.72)
    target_y = int(height * 0.38)
    progress = _clamp(reveal * 1.4)
    draw.line(
        (
            anchor_x,
            anchor_y,
            anchor_x + int((target_x - anchor_x) * progress),
            anchor_y + int((target_y - anchor_y) * progress),
        ),
        fill=_rgba(preset.palette.accent, int(255 * alpha)),
        width=max(2, width // 180),
    )
    draw.ellipse(
        (anchor_x - 7, anchor_y - 7, anchor_x + 7, anchor_y + 7),
        outline=_rgba(preset.palette.accent, int(255 * alpha)),
        width=2,
    )
    panel_w = int(width * 0.40)
    panel_h = int(height * 0.34)
    px = min(width - panel_w, target_x + 12)
    py = max(0, target_y - panel_h // 2)
    _panel(
        draw,
        (px, py, px + panel_w, py + panel_h),
        preset.palette.surface,
        int(238 * alpha),
        radius=8,
    )
    draw.rectangle(
        (px, py, px + max(4, panel_w // 60), py + panel_h),
        fill=_rgba(preset.palette.accent, int(255 * alpha)),
    )
    _draw_text(
        draw,
        spec.text,
        (px + 12, py + 10),
        _font(preset.heading_font, max(12, int(panel_h * 0.28))),
        _rgba(preset.palette.text, int(255 * alpha)),
        panel_w - 24,
        max_lines=2,
        reveal=1.0,
    )


def _draw_diagram(draw, data, box, reveal, preset, alpha):
    x0, y0, x1, y1 = box
    if not data:
        return
    count = len(data)
    gap = (x1 - x0) / max(1, count)
    for index, item in enumerate(data):
        visible = reveal >= index / max(1, count)
        bx0 = x0 + int(gap * index) + 4
        bx1 = x0 + int(gap * (index + 1)) - 8
        _panel(
            draw,
            (bx0, y0, bx1, y1),
            preset.palette.background,
            int((220 if visible else 100) * alpha),
            radius=8,
        )
        label_font = _font(preset.body_font, max(10, int((y1 - y0) * 0.17)))
        _draw_text(
            draw,
            str(item.get("label") or ""),
            (bx0 + 8, y0 + 8),
            label_font,
            _rgba(preset.palette.text, int((255 if visible else 120) * alpha)),
            bx1 - bx0 - 16,
            max_lines=3,
            reveal=1.0,
        )
        if index < count - 1:
            draw.line(
                (bx1 + 2, (y0 + y1) // 2, bx1 + int(gap * 0.22), (y0 + y1) // 2),
                fill=_rgba(preset.palette.accent, int(220 * alpha)),
                width=2,
            )


def _draw_comparison(draw, data, box, reveal, preset, alpha):
    x0, y0, x1, y1 = box
    if not data:
        return
    half = (x1 - x0) // 2
    for index, item in enumerate(data[:2]):
        bx0 = x0 + index * half + (4 if index else 0)
        bx1 = x0 + (index + 1) * half - 4
        _panel(draw, (bx0, y0, bx1, y1), preset.palette.background, int(230 * alpha), radius=8)
        label_font = _font(preset.body_font, max(10, int((y1 - y0) * 0.12)))
        value_font = _font(preset.heading_font, max(18, int((y1 - y0) * 0.28)))
        draw.text(
            (bx0 + 10, y0 + 8),
            str(item.get("label") or "")[:28],
            font=label_font,
            fill=_rgba(preset.palette.muted, int(255 * alpha)),
        )
        if reveal > (0.25 + index * 0.25):
            draw.text(
                (bx0 + 10, y0 + int((y1 - y0) * 0.38)),
                str(item.get("value") or "")[:16],
                font=value_font,
                fill=_rgba(preset.palette.accent, int(255 * alpha)),
            )


def _draw_progress(draw, steps, box, reveal, preset, alpha):
    x0, y0, x1 = box
    count = max(1, len(steps))
    for index, item in enumerate(steps):
        px = x0 + int((x1 - x0) * index / max(1, count - 1))
        draw.ellipse(
            (px - 6, y0 - 6, px + 6, y0 + 6),
            fill=_rgba(
                preset.palette.accent if reveal >= index / count else preset.palette.muted,
                int(255 * alpha),
            ),
        )
        draw.text(
            (px - 12, y0 + 12),
            str(item.get("label") or index + 1)[:18],
            font=_font(preset.body_font, 10),
            fill=_rgba(preset.palette.muted, int(255 * alpha)),
        )


# ------------------------------------------------------------- frame output


def render_overlay_sequence(
    spec: GraphicSpec,
    preset: EditingPreset,
    *,
    width: int,
    height: int,
    fps: int,
    output_dir: Path,
) -> OverlaySequence:
    """Rasterise one graphic to a deterministic transparent PNG sequence."""

    width = max(64, int(width))
    height = max(64, int(height))
    fps = max(1, int(fps))
    duration = max(0.4, float(spec.duration_seconds))
    output_dir.mkdir(parents=True, exist_ok=True)
    frame_count = max(1, round(duration * fps))
    for index in range(frame_count):
        local_t = index / fps
        frame = _draw_graphic(spec, preset, width, height, local_t)
        frame.save(output_dir / f"frame-{index:05d}.png", optimize=True)
    return OverlaySequence(
        directory=output_dir,
        start_seconds=max(0.0, spec.start_seconds),
        duration_seconds=duration,
        fps=fps,
        width=width,
        height=height,
        label=spec.id,
    )


def render_scene_overlays(
    plan: SceneGraphicPlan,
    preset: EditingPreset,
    *,
    width: int,
    height: int,
    fps: int,
    output_dir: Path,
    aspect_ratio: str,
) -> list[OverlaySequence]:
    sequences: list[OverlaySequence] = []
    for spec in sorted(plan.graphics, key=lambda item: (item.layer, item.start_seconds)):
        if not spec.enabled:
            continue
        target = (
            output_dir
            / f"graphic-{spec.scene_position:03d}-{spec.id}-{aspect_ratio.replace(':', 'x')}"
        )
        sequences.append(
            render_overlay_sequence(
                spec,
                preset,
                width=width,
                height=height,
                fps=fps,
                output_dir=target,
            )
        )
    return sequences


def render_graphic_preview(
    spec: GraphicSpec,
    preset: EditingPreset,
    output_path: Path,
    *,
    width: int = 960,
    height: int = 540,
    at_seconds: float | None = None,
) -> Path:
    """Render one still for the editor preview (deterministic sample frame)."""

    local = at_seconds if at_seconds is not None else max(0.4, spec.duration_seconds * 0.55)
    frame = _draw_graphic(spec, preset, width, height, min(local, spec.duration_seconds - 0.01))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.save(output_path)
    return output_path


def apply_saved_graphics(
    plan: SceneGraphicPlan,
    saved_plan: dict[str, Any] | None,
) -> SceneGraphicPlan:
    """Overlay operator-edited graphics onto a freshly planned scene.

    Automatic planning must never silently discard an edit. Only specs marked
    ``edited`` are restored; untouched automatic specs are replaced by the new
    plan so improved planning still applies to the rest of the scene.
    """

    if not saved_plan:
        return plan
    saved = [
        GraphicSpec.from_dict(item)
        for item in saved_plan.get("graphics") or []
        if item.get("edited")
    ]
    if not saved:
        return plan
    merged = {graphic.id: graphic for graphic in plan.graphics}
    for graphic in saved:
        merged[graphic.id] = graphic
    ordered = sorted(merged.values(), key=lambda item: (item.layer, item.start_seconds))
    return SceneGraphicPlan(
        scene_position=plan.scene_position,
        purpose=plan.purpose,
        shot=plan.shot,
        movement=plan.movement,
        transition=plan.transition,
        pacing=plan.pacing,
        rationale=plan.rationale,
        graphics=ordered,
        checks=plan.checks,
    )


def plan_graphics_for_timeline(
    *,
    scenes: Iterable[Any],
    timings: Iterable[Any],
    preset: EditingPreset,
    profile: Any,
    asset_lookup: dict[int, tuple[bool, str]],
    aspect_ratio: str,
    captions_enabled: bool,
    frame_types: dict[int, str] | None = None,
) -> list[SceneGraphicPlan]:
    frame_types = frame_types or {}
    plans: list[SceneGraphicPlan] = []
    timing_by_position: dict[int, Any] = {}
    scenes_by_position: dict[int, Any] = {}
    for scene, timing in zip(scenes, timings, strict=False):
        timing_by_position[scene.position] = timing
        scenes_by_position[scene.position] = scene
        has_asset, asset_kind = asset_lookup.get(scene.position, (False, "graphic"))
        plans.append(
            plan_scene_graphics(
                scene_position=scene.position,
                start_seconds=timing.start_seconds,
                duration_seconds=timing.duration_seconds,
                narration=scene.narration,
                subtitle=scene.subtitle,
                keywords=list(scene.keywords or []),
                preset=preset,
                profile=profile,
                has_asset=has_asset,
                asset_kind=asset_kind,
                aspect_ratio=aspect_ratio,
                captions_enabled=captions_enabled,
                frame_type=frame_types.get(scene.position, ""),
            )
        )
    if frame_types:
        _limit_panel_overlays(plans, frame_types)
        # A dropped panel must not leave a designed frame with dead space. Give
        # it a real identification when the narration names someone.
        for plan in plans:
            timing = timing_by_position.get(plan.scene_position)
            scene = scenes_by_position.get(plan.scene_position)
            if plan.graphics or timing is None or scene is None:
                continue
            if frame_types.get(plan.scene_position) not in {"typography", "photo"}:
                continue
            spec = _identification_spec(
                scene_position=plan.scene_position,
                start_seconds=timing.start_seconds,
                duration_seconds=timing.duration_seconds,
                narration=scene.narration,
                preset=preset,
                profile=profile,
                aspect_ratio=aspect_ratio,
                captions_enabled=captions_enabled,
            )
            if spec is not None:
                plan.graphics = [spec]
        # Re-run the safe-area checks against what will actually be composited.
        for plan in plans:
            timing = timing_by_position.get(plan.scene_position)
            if timing is None:
                continue
            plan.checks = graphic_checks(
                plan.graphics,
                aspect_ratio=aspect_ratio,
                captions_enabled=captions_enabled,
                scene_start=timing.start_seconds,
                scene_end=timing.start_seconds + timing.duration_seconds,
            )
    return plans
