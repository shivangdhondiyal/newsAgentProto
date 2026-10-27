from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageEnhance

from backend.app.media.presets import CraftRules, EditingPreset

# Shot grammar from the editing guide: a sequence reads better when the frame
# size changes deliberately instead of repeating one crop for the whole story.
SHOT_CROP_FACTOR: dict[str, float] = {
    "wide": 1.0,
    "wide-map": 0.92,
    "wide-diagram": 0.94,
    "medium": 1.12,
    "medium-map": 1.1,
    "medium-graphic": 1.12,
    "medium-chart": 1.1,
    "medium-diagram": 1.12,
    "close": 1.28,
    "close-stat": 1.24,
    "close-label": 1.2,
    "evidence": 1.06,
    "document": 1.04,
    "typography": 1.0,
    "split": 1.0,
    "interview": 1.1,
}

# Only transitions that FFmpeg's xfade filter actually exposes.
FFMPEG_TRANSITIONS = {
    "fade",
    "fadeblack",
    "fadewhite",
    "dissolve",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "slideleft",
    "slideright",
    "slideup",
    "slidedown",
    "smoothleft",
    "smoothright",
    "circleopen",
    "circleclose",
    "radial",
    "cut",
}

# The guide warns against decorating every cut. These are the restrained
# defaults for a hard cut and for a deliberate scene change.
HARD_CUT_TRANSITIONS = {"cut", "fade"}
RESTRAINED_LIMIT_RATIO = 0.35


@dataclass(frozen=True)
class CaptionRules:
    """Platform-aware caption limits that keep text inside a safe area."""

    max_characters: int
    max_lines: int
    safe_bottom_ratio: float
    font_scale: float
    note: str


def shot_for_position(position: int, rules: CraftRules) -> str:
    """Cycle the preset's shot mix so consecutive scenes change frame size."""

    if not rules.shot_mix:
        return "medium"
    index = max(0, int(position) - 1) % len(rules.shot_mix)
    return rules.shot_mix[index]


def crop_factor(shot: str) -> float:
    return SHOT_CROP_FACTOR.get(shot, 1.0)


def crop_box(
    size: tuple[int, int],
    *,
    shot: str,
    aspect_ratio: str = "16:9",
) -> tuple[int, int, int, int]:
    """Return a centred crop rectangle for a shot size, without ever upscaling."""

    width, height = size
    factor = crop_factor(shot)
    # Portrait formats need more headroom, so the crop tightens less aggressively.
    if aspect_ratio == "9:16":
        factor = 1.0 + (factor - 1.0) * 0.7
    # A "wide" shot factor below 1 would ask for a rectangle larger than the
    # source; PIL then pads it with black, which showed up as a black bar on
    # the right of the frame. Never crop outside the image.
    crop_w = min(width, max(1, round(width / factor)))
    crop_h = min(height, max(1, round(height / factor)))
    left = max(0, (width - crop_w) // 2)
    top = max(0, (height - crop_h) // 2)
    return left, top, left + crop_w, top + crop_h


def grade_image(image: Image.Image, rules: CraftRules) -> Image.Image:
    """Apply colour correction before any stylistic grade, as the guide advises."""

    corrected = ImageEnhance.Brightness(image).enhance(max(0.5, rules.exposure))
    corrected = ImageEnhance.Contrast(corrected).enhance(max(0.5, rules.contrast))
    corrected = ImageEnhance.Color(corrected).enhance(max(0.0, rules.saturation))
    if abs(rules.temperature) >= 0.01:
        corrected = _shift_temperature(corrected, rules.temperature)
    return corrected


def _shift_temperature(image: Image.Image, amount: float) -> Image.Image:
    """Warm or cool the image with a gentle channel gain, not a tint overlay."""

    amount = max(-0.5, min(0.5, amount))
    red_gain = 1.0 + amount * 0.18
    blue_gain = 1.0 - amount * 0.18
    bands = list(image.convert("RGB").split())
    bands[0] = bands[0].point(lambda value: _clamp(value * red_gain))
    bands[2] = bands[2].point(lambda value: _clamp(value * blue_gain))
    return Image.merge("RGB", bands)


def _clamp(value: float) -> int:
    return max(0, min(255, round(value)))


def transition_for_scene(
    base_transition: str,
    rules: CraftRules,
    *,
    scene_index: int,
    is_boundary: bool,
) -> str:
    """Use a cut inside a sequence and a deliberate transition at boundaries."""

    candidate = (base_transition or "fade").strip().lower()
    if candidate not in FFMPEG_TRANSITIONS:
        candidate = "fade"
    if not is_boundary:
        return "cut" if rules.cut_rhythm in {"quick", "beat"} else candidate
    # Boundary transitions should be readable, not decorative.
    if rules.cut_rhythm == "quick":
        return "cut"
    return candidate


def validate_transition_restraint(transitions: list[str]) -> list[str]:
    """Warn when decorative transitions dominate the timeline."""

    if not transitions:
        return []
    decorative = [
        item
        for item in transitions
        if item not in HARD_CUT_TRANSITIONS and item not in {"dissolve", "wipeleft", "wiperight"}
    ]
    ratio = len(decorative) / len(transitions)
    if ratio > RESTRAINED_LIMIT_RATIO:
        return [
            (
                f"{round(ratio * 100)}% of transitions are decorative; the editing guide "
                "recommends cuts for most changes and effects only at real boundaries."
            )
        ]
    return []


def ease_progress(progress: float, easing: str = "ease_in_out") -> float:
    """Map linear progress to the eased keyframe curves used by motion graphics."""

    value = max(0.0, min(1.0, float(progress)))
    if easing in {"linear", ""}:
        return value
    if easing in {"ease_in", "ease_in_quad"}:
        return value * value
    if easing in {"ease_out", "ease_out_quad"}:
        return 1.0 - (1.0 - value) ** 2
    if easing in {"ease_out_cubic", "snap"}:
        return 1.0 - (1.0 - value) ** 3
    # ease_in_out (sine) is the default: slow in, slow out, no linear snap.
    return 0.5 - math.cos(math.pi * value) / 2.0


def motion_offset(
    progress: float,
    *,
    intensity: float,
    easing: str,
    path: str = "linear",
) -> tuple[float, float]:
    """Return a small normalized pan offset for a Ken Burns / parallax move."""

    eased = ease_progress(progress, easing)
    travel = max(0.0, min(1.0, intensity)) * 0.06
    if path == "drift":
        return (eased - 0.5) * travel, math.sin(eased * math.pi) * travel * 0.4
    if path == "push":
        return 0.0, (0.5 - eased) * travel
    return (eased - 0.5) * travel, 0.0


def music_gain_for(rules: CraftRules, preset_music: str) -> float:
    """Respect the preset's intent and the craft rule's gain ceiling."""

    if preset_music == "none" or rules.music_gain <= 0:
        return 0.0
    return round(min(rules.music_gain, 0.16), 4)


def sfx_gain_for(rules: CraftRules, preset_sfx: str) -> float:
    if preset_sfx == "none" or rules.sfx_gain <= 0:
        return 0.0
    return round(min(rules.sfx_gain, 0.08), 4)


def platform_caption_rules(rules: CraftRules, aspect_ratio: str) -> CaptionRules:
    """Aspect-specific caption limits that keep text clear of platform UI."""

    priority = rules.caption_priority
    if aspect_ratio == "9:16":
        # The guide and platform specs both reserve the lower ~18% for UI.
        return CaptionRules(
            max_characters=28 if priority == "high" else 34,
            max_lines=2,
            safe_bottom_ratio=0.18,
            font_scale=1.0,
            note="Vertical captions stay above the bottom 18% and clear of right-side UI.",
        )
    return CaptionRules(
        max_characters=42 if priority == "high" else 52,
        max_lines=2,
        safe_bottom_ratio=0.08,
        font_scale=0.92,
        note="Landscape captions stay above the bottom 8% title-safe band.",
    )


def safe_margin(preset: EditingPreset, aspect_ratio: str) -> float:
    layout = preset.landscape if aspect_ratio == "16:9" else preset.portrait
    # Craft rules can demand a wider margin than the layout default.
    if "title-safe" in preset.craft.safe_area_note.lower():
        return max(layout.safe_margin_ratio, 0.06)
    return layout.safe_margin_ratio


def crop_focal(
    image: Image.Image,
    *,
    shot: str,
    focal_x: float,
    focal_y: float,
    aspect_ratio: str = "16:9",
) -> Image.Image:
    """Crop to a shot size around the detected focal point, preserving aspect."""

    box = crop_box(image.size, shot=shot, aspect_ratio=aspect_ratio)
    width, height = image.size
    crop_w = box[2] - box[0]
    crop_h = box[3] - box[1]
    left = round(focal_x * width - crop_w / 2)
    top = round(focal_y * height - crop_h / 2)
    left = max(0, min(width - crop_w, left))
    top = max(0, min(height - crop_h, top))
    return image.crop((left, top, left + crop_w, top + crop_h))
