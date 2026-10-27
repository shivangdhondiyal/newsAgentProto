from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from backend.app.media.craft import (
    crop_focal,
    grade_image,
    platform_caption_rules,
    shot_for_position,
)
from backend.app.media.presets import EditingPreset, LayoutRules, Palette

FONT_DIRS = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/System/Library/Fonts/Supplemental"),
)
FONT_FALLBACKS = {
    "arial.ttf": ("arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"),
    "arialbd.ttf": ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Helvetica.ttc"),
    "georgia.ttf": ("georgia.ttf", "DejaVuSerif.ttf", "Times New Roman.ttf"),
    "georgiab.ttf": ("georgiab.ttf", "DejaVuSerif-Bold.ttf", "Times New Roman Bold.ttf"),
    "consola.ttf": ("consola.ttf", "DejaVuSansMono.ttf", "Courier New.ttf"),
    "consolab.ttf": ("consolab.ttf", "DejaVuSansMono-Bold.ttf", "Courier New Bold.ttf"),
    "impact.ttf": ("impact.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"),
}

FIGURE_RE = re.compile(
    r"(?<![\w.])([$€£]?\d[\d,]*(?:\.\d+)?\s?(?:%|percent|million|billion|thousand|"
    r"km|kilometres|kilometers|miles|people|years|days|hours)?)",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
GEO_TERMS = {
    "border",
    "city",
    "coast",
    "country",
    "district",
    "east",
    "geopolitical",
    "island",
    "map",
    "north",
    "province",
    "region",
    "route",
    "south",
    "state",
    "west",
    "world",
}
ILLUSTRATIVE_NOTE = "Illustrative locator · verify against reporting"


@dataclass
class SceneVisual:
    position: int
    start_seconds: float
    duration_seconds: float
    narration: str
    subtitle: str
    headline: str = ""
    keywords: list[str] = field(default_factory=list)
    scene_description: str = ""
    image_query: str = ""
    animation_suggestion: str = ""
    transition: str = "fade"
    asset_path: Path | None = None
    asset_kind: str = "graphic"
    attribution: str | None = None
    source_url: str | None = None
    focal_x: float = 0.5
    focal_y: float = 0.5
    graphic_type: str = "auto"
    shot: str = ""


@dataclass(frozen=True)
class FrameResult:
    path: Path
    graphic_type: str
    has_asset: bool
    headline: str
    source_text: str


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    red, green, blue = _rgb(value)
    return red, green, blue, alpha


def _font_path(name: str) -> Path | None:
    for candidate in FONT_FALLBACKS.get(name, (name,)):
        for directory in FONT_DIRS:
            path = directory / candidate
            if path.is_file():
                return path
    return None


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_path(name)
    if path is None:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(str(path), max(8, size))
    except OSError:
        return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
    *,
    max_lines: int = 5,
) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    truncated = False
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _text_size(draw, candidate, font)[0] > max_width:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                truncated = True
                current = ""
                break
        else:
            current = candidate
    if current and len(lines) < max_lines:
        lines.append(current)
    if truncated and lines:
        last = lines[-1]
        while last and _text_size(draw, f"{last}…", font)[0] > max_width:
            last = last[:-1]
        lines[-1] = f"{last.rstrip(' ,.;:')}…"
    return lines


def _fits_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
    max_lines: int,
) -> bool:
    """True when the text wraps within the line budget without clipping."""

    words = str(text or "").split()
    if not words:
        return True
    lines = 0
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _text_size(draw, candidate, font)[0] > max_width:
            lines += 1
            current = word
            if lines >= max_lines:
                return False
        else:
            current = candidate
    return lines + (1 if current else 0) <= max_lines


def _fit_headline_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    family: str,
    size: int,
    max_width: int,
    max_lines: int,
):
    """Shrink the headline until the whole sentence fits its line budget."""

    size = max(20, int(size))
    font = _font(family, size)
    while size > 20 and not _fits_lines(draw, text, font, max_width, max_lines):
        size = max(20, int(size * 0.92))
        font = _font(family, size)
    return font


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font,
    fill: tuple[int, int, int],
    max_width: int,
    *,
    line_gap: float = 1.14,
    max_lines: int = 5,
    stroke: int = 0,
) -> int:
    lines = wrap_text(draw, text, font, max_width, max_lines=max_lines)
    x, y = xy
    _, height = _text_size(draw, "Ag", font)
    step = int(height * line_gap)
    for line in lines:
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke,
            stroke_fill=(0, 0, 0),
        )
        y += step
    return y


def _gradient(size: tuple[int, int], top: str, bottom: str) -> Image.Image:
    width, height = size
    top_rgb = _rgb(top)
    bottom_rgb = _rgb(bottom)
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(
            round(top_rgb[index] + (bottom_rgb[index] - top_rgb[index]) * ratio)
            for index in range(3)
        )
        draw.line((0, y, width, y), fill=color)
    return image


def _cover_crop(
    image: Image.Image,
    box: tuple[int, int],
    focal_x: float,
    focal_y: float,
) -> Image.Image:
    box_w, box_h = box
    source = image.convert("RGB")
    scale = max(box_w / source.width, box_h / source.height)
    new_w = max(box_w, round(source.width * scale))
    new_h = max(box_h, round(source.height * scale))
    resized = source.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = round(max(0.0, min(1.0, focal_x)) * new_w - box_w / 2)
    top = round(max(0.0, min(1.0, focal_y)) * new_h - box_h / 2)
    left = max(0, min(new_w - box_w, left))
    top = max(0, min(new_h - box_h, top))
    return resized.crop((left, top, left + box_w, top + box_h))


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return mask


def _paste_rounded(
    canvas: Image.Image,
    image: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
) -> None:
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    fitted = image.resize((width, height), Image.Resampling.LANCZOS)
    canvas.paste(fitted, (box[0], box[1]), _rounded_mask((width, height), radius))


def _overlay_gradient(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    *,
    color: str = "#000000",
    top_alpha: int = 35,
    bottom_alpha: int = 220,
) -> None:
    x0, y0, x1, y1 = box
    height = max(1, y1 - y0)
    overlay = Image.new("RGBA", (max(1, x1 - x0), height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    rgb = _rgb(color)
    for y in range(height):
        ratio = y / max(1, height - 1)
        alpha = round(top_alpha + (bottom_alpha - top_alpha) * ratio)
        draw.line((0, y, overlay.width, y), fill=(*rgb, alpha))
    canvas.alpha_composite(overlay, (x0, y0))


def _panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    palette: Palette,
    *,
    alpha: int = 230,
    radius: int = 18,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=_rgba(palette.surface, alpha))


def extract_percentages(text: str) -> list[tuple[str, float]]:
    values: list[tuple[str, float]] = []
    for match in PERCENT_RE.finditer(text or ""):
        value = float(match.group(1))
        context = _figure_context(text, match.start())
        values.append((context, value))
    return values


def extract_figures(text: str) -> list[tuple[str, str]]:
    figures: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in FIGURE_RE.finditer(text or ""):
        token = " ".join(match.group(1).split())
        if not token or token in seen:
            continue
        if YEAR_RE.fullmatch(token):
            continue
        context = _figure_context(text, match.start())
        figures.append((context, token))
        seen.add(token)
        if len(figures) >= 4:
            break
    return figures


def extract_years(text: str) -> list[str]:
    years: list[str] = []
    for match in YEAR_RE.finditer(text or ""):
        year = match.group(0)
        if year not in years:
            years.append(year)
    return years


def _figure_context(text: str, start: int) -> str:
    prefix = (text or "")[:start].strip()
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", prefix)
    return " ".join(words[-4:]) or "Reported figure"


def choose_graphic_type(visual: SceneVisual, preset: EditingPreset) -> str:
    if visual.graphic_type and visual.graphic_type != "auto":
        return visual.graphic_type
    text = " ".join(
        [visual.narration, visual.scene_description, visual.animation_suggestion]
    ).lower()
    years = extract_years(visual.narration)
    percentages = extract_percentages(visual.narration)
    figures = extract_figures(visual.narration)
    if "map" in preset.graphics and (
        any(term in text for term in GEO_TERMS) or "map" in preset.key
    ):
        return "map"
    if "split" in preset.graphics and preset.key == "split_screen":
        return "split"
    if "quote" in preset.graphics and preset.key == "interview":
        return "quote"
    if "timeline" in preset.graphics and len(years) >= 2:
        return "timeline"
    if "chart" in preset.graphics and len(percentages) >= 2:
        return "chart"
    if "diagram" in preset.graphics and preset.key in {"diagram", "vox"}:
        # A licensed photo of the actual subject beats a three-word keyword
        # diagram. Keep the diagram for scenes that carry structure a graphic
        # can explain (dates, figures, percentages).
        has_asset = bool(visual.asset_path and visual.asset_path.is_file())
        if has_asset and not (years or figures or percentages):
            return "photo"
        return "diagram"
    if "stat" in preset.graphics and figures:
        return "stat"
    if preset.key in {"vox", "data", "diagram", "map_geopolitical"}:
        if len(years) >= 2:
            return "timeline"
        if figures:
            return "stat"
        return "diagram"
    if visual.asset_path and visual.asset_path.is_file():
        return "photo"
    return "typography"


_TRAILING_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "nor",
        "in",
        "on",
        "at",
        "of",
        "to",
        "for",
        "with",
        "as",
        "by",
        "from",
        "that",
        "this",
        "these",
        "those",
        "was",
        "were",
        "is",
        "are",
        "be",
        "been",
        "has",
        "have",
        "had",
        "will",
        "would",
        "could",
        "should",
        "its",
        "it",
        "his",
        "her",
        "their",
        "our",
        "your",
        "than",
        "then",
        "while",
        "when",
        "where",
        "which",
        "who",
        "whose",
        "after",
        "before",
        "during",
        "over",
        "under",
        "about",
        "into",
    }
)


def _truncate_headline(text: str, limit: int) -> str:
    """Cut a headline at a sentence or clause boundary, never mid-phrase."""

    if len(text) <= limit:
        return text
    clipped = text[:limit]
    # Prefer a complete sentence, then a clause break, before a word break.
    for separator in (". ", "? ", "! ", "; ", ", "):
        index = clipped.rfind(separator)
        if index >= max(24, int(limit * 0.45)):
            return clipped[:index].rstrip(" ,;:-") + "…"
    clipped = clipped.rsplit(" ", 1)[0].rstrip(" ,;:-")
    words = clipped.split()
    while len(words) > 3 and words[-1].lower().strip(",;:.") in _TRAILING_FUNCTION_WORDS:
        words.pop()
    return " ".join(words) + "…"


def _headline_for(
    visual: SceneVisual, preset: EditingPreset, layout: LayoutRules | None = None
) -> str:
    headline = (visual.headline or visual.subtitle or visual.narration or "").strip()
    headline = " ".join(headline.split())
    limit = (layout or preset.landscape).max_headline_chars
    return _truncate_headline(headline, limit * 2)


def _caption_repeats_headline(
    visual: SceneVisual, preset: EditingPreset, layout: LayoutRules | None = None
) -> bool:
    """True when the burned-in caption would print the headline a second time."""

    caption = " ".join((visual.subtitle or visual.narration or "").split()).lower()
    headline = _headline_for(visual, preset, layout).lower()
    if not caption or not headline:
        return False
    probe = headline.rstrip(" .…")[:32]
    return len(probe) >= 20 and caption.startswith(probe)


def _kicker_for(visual: SceneVisual) -> str:
    if visual.keywords:
        return " · ".join(str(item).upper() for item in visual.keywords[:3])
    return f"SCENE {visual.position:02d}"


def _fit_kicker_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
) -> str:
    """Fit a kicker to its slot, dropping whole keywords before cutting a word."""

    label = text.upper()
    width, _ = _text_size(draw, label, font)
    if width > max_width and " · " in label:
        segments = label.split(" · ")
        while len(segments) > 1:
            segments.pop()
            candidate = " · ".join(segments)
            width, _ = _text_size(draw, candidate, font)
            if width <= max_width:
                break
        label = " · ".join(segments)
    if width > max_width:
        label = label[: max(3, int(len(label) * max_width / max(1, width)) - 1)] + "…"
    return label


def _draw_kicker(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    preset: EditingPreset,
    size: int,
    max_width: int,
) -> int:
    font = _font(preset.body_font, size)
    x, y = xy
    label = _fit_kicker_label(draw, text, font, max_width)
    width, height = _text_size(draw, label, font)
    if preset.accent_rule == "band":
        draw.rectangle(
            (x - 14, y - 6, x + width + 14, y + height + 8),
            fill=_rgb(preset.palette.accent),
        )
        draw.text((x, y), label, font=font, fill=_rgb("#111111"))
    else:
        draw.rectangle((x, y + 2, x + 4, y + height), fill=_rgb(preset.palette.accent))
        draw.text((x + 14, y), label, font=font, fill=_rgb(preset.palette.accent))
    return y + height + 18


def _draw_source(
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    width: int,
    height: int,
    margin: int,
    size: int,
    *,
    fallback_note: str | None = None,
) -> str:
    if not preset.show_source:
        return ""
    if visual.attribution:
        text = f"Source: {visual.attribution}"
    elif visual.source_url:
        text = f"Source: {visual.source_url}"
    elif fallback_note:
        text = fallback_note
    else:
        text = "Designed graphic · no external asset used"
    font = _font(preset.body_font, max(12, size))
    max_width = int(width * 0.52)
    lines = wrap_text(draw, text, font, max_width, max_lines=2)
    y = height - margin - len(lines) * int(size * 1.25)
    for line in lines:
        draw.text((margin, y), line, font=font, fill=_rgb(preset.palette.muted))
        y += int(size * 1.25)
    return text


def _draw_headline(
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    layout: LayoutRules,
    width: int,
    height: int,
    margin: int,
    *,
    max_width: int | None = None,
    max_lines: int = 4,
    on_photo: bool = False,
    kicker_reserve: int = 0,
) -> int:
    headline = _headline_for(visual, preset, layout)
    if not headline:
        return margin
    scale = preset.headline_scale * (0.82 if height > width else 1.0)
    kicker_size = max(13, int(height * preset.kicker_scale))
    available = max_width or (width - margin * 2)
    # Shrink to fit the line budget instead of ellipsizing a long headline; the
    # cut-off text is the part a viewer actually needed to read.
    font = _fit_headline_font(
        draw,
        headline,
        preset.heading_font,
        max(20, int(height * scale)),
        available,
        max_lines,
    )
    if layout.headline_position == "top":
        y = margin
    elif layout.headline_position == "center":
        y = int(height * 0.30)
    else:
        y = int(height * 0.62)
    y = _draw_kicker(
        draw,
        _kicker_for(visual),
        (margin, y),
        preset,
        kicker_size,
        max(80, available - kicker_reserve),
    )
    stroke = 2 if preset.key in {"social", "kinetic_type", "breaking"} else 0
    fill = _rgb(preset.palette.text)
    if on_photo:
        # Dark palette text disappears over photography; use a light headline
        # with a hard shadow so it reads against any exposure.
        fill = (255, 255, 255)
        stroke = max(stroke, 3)
    return _draw_wrapped(
        draw,
        headline,
        (margin, y),
        font,
        fill,
        available,
        line_gap=1.08,
        max_lines=max_lines,
        stroke=stroke,
    )


def _draw_lower_third(
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    width: int,
    height: int,
    margin: int,
) -> None:
    if not preset.lower_third:
        return
    title = " ".join((visual.headline or visual.subtitle or visual.narration).split())[:110]
    detail = " · ".join(str(item) for item in visual.keywords[:3])
    title_font = _font(preset.heading_font, max(16, int(height * 0.030)))
    detail_font = _font(preset.body_font, max(13, int(height * 0.021)))
    box_width = min(int(width * 0.68), int(width - margin * 2))
    box_height = int(height * 0.13)
    x0 = margin
    y0 = height - margin - box_height - int(height * 0.045)
    _panel(draw, (x0, y0, x0 + box_width, y0 + box_height), preset.palette, alpha=235, radius=12)
    draw.rectangle(
        (x0, y0, x0 + max(6, int(width * 0.006)), y0 + box_height),
        fill=_rgb(preset.palette.accent),
    )
    text_x = x0 + int(width * 0.022)
    _draw_wrapped(
        draw,
        title,
        (text_x, y0 + int(box_height * 0.18)),
        title_font,
        _rgb(preset.palette.text),
        box_width - int(width * 0.04),
        max_lines=2,
    )
    if detail:
        draw.text(
            (text_x, y0 + int(box_height * 0.68)),
            detail,
            font=detail_font,
            fill=_rgb(preset.palette.muted),
        )


def _draw_ticker(
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    width: int,
    height: int,
    margin: int,
) -> None:
    if not preset.ticker:
        return
    band_height = max(28, int(height * 0.040))
    y0 = height - band_height
    draw.rectangle((0, y0, width, height), fill=_rgb(preset.palette.accent))
    text = f"{_kicker_for(visual)}   |   {visual.subtitle or visual.narration}"
    text = " ".join(text.split())
    font = _font(preset.body_font, max(12, int(band_height * 0.48)))
    draw.text((margin, y0 + int(band_height * 0.22)), text[:180], font=font, fill=_rgb("#0B0B0D"))


def _draw_caption(
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    width: int,
    height: int,
    margin: int,
    *,
    aspect_ratio: str = "16:9",
) -> None:
    text = " ".join((visual.subtitle or visual.narration or "").split())
    if not text or preset.caption_style == "minimal":
        return
    caption_rules = platform_caption_rules(preset.craft, aspect_ratio)
    if len(text) > caption_rules.max_characters:
        # Prefer a complete first sentence over a mid-clause cut.
        first_sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        if 24 <= len(first_sentence) <= caption_rules.max_characters:
            text = first_sentence
        else:
            text = text[: caption_rules.max_characters].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    size = max(14, int(height * preset.caption_scale * caption_rules.font_scale))
    font = _font(preset.body_font, size)
    max_width = int(width * (0.82 if aspect_ratio == "9:16" else 0.72))
    lines = wrap_text(draw, text, font, max_width, max_lines=caption_rules.max_lines)
    if not lines:
        return
    line_height = int(size * 1.28)
    box_height = line_height * len(lines) + int(size * 0.9)
    if preset.caption_style == "centered":
        y0 = int(height * (1.0 - caption_rules.safe_bottom_ratio - 0.08)) - box_height
        x0 = (width - max_width) // 2 - int(size * 0.6)
    else:
        y0 = height - margin - box_height - int(height * caption_rules.safe_bottom_ratio)
        x0 = margin
    box_width = max_width + int(size * 1.2)
    if preset.caption_style in {"boxed", "centered", "kinetic"}:
        _panel(
            draw,
            (x0, y0, x0 + box_width, y0 + box_height),
            preset.palette,
            alpha=225,
            radius=10,
        )
    y = y0 + int(size * 0.45)
    for line in lines:
        if preset.caption_style == "centered":
            line_width = _text_size(draw, line, font)[0]
            x = (width - line_width) // 2
        else:
            x = x0 + int(size * 0.6)
        draw.text(
            (x, y),
            line,
            font=font,
            fill=_rgb(preset.palette.text),
            stroke_width=1,
            stroke_fill=(0, 0, 0),
        )
        y += line_height


def _draw_photo_frame(
    canvas: Image.Image,
    visual: SceneVisual,
    preset: EditingPreset,
    width: int,
    height: int,
    margin: int,
    *,
    aspect_ratio: str = "16:9",
) -> bool:
    if not visual.asset_path or not visual.asset_path.is_file():
        return False
    try:
        with Image.open(visual.asset_path) as source:
            source_image = source.convert("RGB")
            shot = visual.shot or shot_for_position(visual.position, preset.craft)
            framed = crop_focal(
                source_image,
                shot=shot,
                focal_x=visual.focal_x,
                focal_y=visual.focal_y,
                aspect_ratio=aspect_ratio,
            )
            image = _cover_crop(framed, (width, height), visual.focal_x, visual.focal_y)
    except (OSError, ValueError):
        return False
    image = grade_image(image, preset.craft)
    canvas.paste(image, (0, 0))
    _overlay_gradient(canvas, (0, 0, width, height), top_alpha=40, bottom_alpha=235)
    # The kicker and headline sit over live photography, so a dedicated top
    # scrim guarantees contrast on a bright sky or a dark crowd alike.
    _overlay_gradient(
        canvas,
        (0, 0, width, int(height * 0.58)),
        top_alpha=175,
        bottom_alpha=0,
    )
    if preset.vignette:
        _vignette(canvas, preset.vignette)
    return True


def _vignette(canvas: Image.Image, strength: float) -> None:
    width, height = canvas.size
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        (-width * 0.15, -height * 0.15, width * 1.15, height * 1.15),
        fill=255,
    )
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(8, width // 30)))
    dark = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = mask.load()
    dark_pixels = dark.load()
    amount = int(max(0.0, min(1.0, strength)) * 170)
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            alpha = amount - int(pixels[x, y] / 255 * amount)
            if alpha > 0:
                dark_pixels[x, y] = (0, 0, 0, alpha)
    canvas.alpha_composite(dark)


def _draw_map_panel(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    box: tuple[int, int, int, int],
    margin: int,
) -> None:
    x0, y0, x1, y1 = box
    _panel(draw, box, preset.palette, alpha=245, radius=16)
    inner = (x0 + margin // 2, y0 + margin // 2, x1 - margin // 2, y1 - margin // 2)
    grid_color = _rgba(preset.palette.muted, 70)
    for index in range(1, 6):
        x = inner[0] + (inner[2] - inner[0]) * index / 6
        draw.line((x, inner[1], x, inner[3]), fill=grid_color, width=1)
    for index in range(1, 4):
        y = inner[1] + (inner[3] - inner[1]) * index / 4
        draw.line((inner[0], y, inner[2], y), fill=grid_color, width=1)
    cx = (inner[0] + inner[2]) // 2
    cy = (inner[1] + inner[3]) // 2
    radius = int(min(inner[2] - inner[0], inner[3] - inner[1]) * 0.28)
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=_rgba(preset.palette.secondary, 180),
        width=max(2, radius // 18),
    )
    draw.arc(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        start=200,
        end=340,
        fill=_rgba(preset.palette.accent, 170),
        width=max(2, radius // 18),
    )
    marker_x = cx + int(radius * 0.42)
    marker_y = cy - int(radius * 0.34)
    draw.ellipse(
        (marker_x - 10, marker_y - 10, marker_x + 10, marker_y + 10),
        fill=_rgb(preset.palette.accent),
    )
    draw.ellipse(
        (marker_x - 22, marker_y - 22, marker_x + 22, marker_y + 22),
        outline=_rgba(preset.palette.accent, 160),
        width=2,
    )
    label = (visual.keywords[0] if visual.keywords else "Location").upper()
    label_font = _font(preset.heading_font, max(14, int((y1 - y0) * 0.062)))
    draw.text(
        (inner[0], inner[3] - int((y1 - y0) * 0.22)),
        label[:26],
        font=label_font,
        fill=_rgb(preset.palette.text),
    )
    note_font = _font(preset.body_font, max(10, int((y1 - y0) * 0.038)))
    draw.text(
        (inner[0], inner[3] - int((y1 - y0) * 0.09)),
        ILLUSTRATIVE_NOTE,
        font=note_font,
        fill=_rgb(preset.palette.muted),
    )
    title_font = _font(preset.body_font, max(12, int((y1 - y0) * 0.055)))
    draw.text(
        (inner[0], inner[1]),
        "LOCATION CONTEXT",
        font=title_font,
        fill=_rgb(preset.palette.accent),
    )


def _draw_stat_panel(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    box: tuple[int, int, int, int],
    margin: int,
) -> None:
    x0, y0, x1, y1 = box
    _panel(draw, box, preset.palette, alpha=245, radius=16)
    figures = extract_figures(visual.narration)
    title_font = _font(preset.body_font, max(12, int((y1 - y0) * 0.055)))
    draw.text(
        (x0 + margin // 2, y0 + margin // 2),
        "FIGURES CITED IN NARRATION",
        font=title_font,
        fill=_rgb(preset.palette.accent),
    )
    if not figures:
        figures = [("Reported detail", (visual.keywords or ["Context"])[0])]
    count = min(3, len(figures))
    row_height = (y1 - y0 - margin) / max(1, count)
    for index, (context, value) in enumerate(figures[:count]):
        row_y = y0 + margin // 2 + int(row_height * index) + int((y1 - y0) * 0.09)
        value_font = _font(preset.heading_font, max(20, int(row_height * 0.46)))
        label_font = _font(preset.body_font, max(11, int(row_height * 0.18)))
        draw.text(
            (x0 + margin // 2, row_y),
            value,
            font=value_font,
            fill=_rgb(preset.palette.text),
        )
        draw.text(
            (x0 + margin // 2, row_y + int(row_height * 0.45)),
            context[:42],
            font=label_font,
            fill=_rgb(preset.palette.muted),
        )
        if index < count - 1:
            line_y = row_y + int(row_height * 0.78)
            draw.line(
                (x0 + margin // 2, line_y, x1 - margin // 2, line_y),
                fill=_rgba(preset.palette.muted, 55),
            )


def _draw_chart_panel(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    box: tuple[int, int, int, int],
    margin: int,
) -> None:
    x0, y0, x1, y1 = box
    _panel(draw, box, preset.palette, alpha=245, radius=16)
    values = extract_percentages(visual.narration)
    title_font = _font(preset.body_font, max(12, int((y1 - y0) * 0.055)))
    draw.text(
        (x0 + margin // 2, y0 + margin // 2),
        "PERCENTAGES CITED IN NARRATION",
        font=title_font,
        fill=_rgb(preset.palette.accent),
    )
    if len(values) < 2:
        _draw_stat_panel(canvas, draw, visual, preset, box, margin)
        return
    chart_left = x0 + margin // 2
    chart_right = x1 - margin // 2
    chart_top = y0 + int((y1 - y0) * 0.16)
    chart_bottom = y1 - int((y1 - y0) * 0.16)
    available = chart_bottom - chart_top
    row_height = available / len(values)
    bar_max = chart_right - chart_left - int((x1 - x0) * 0.12)
    label_font = _font(preset.body_font, max(11, int(row_height * 0.20)))
    value_font = _font(preset.body_font, max(11, int(row_height * 0.20)))
    for index, (context, value) in enumerate(values[:6]):
        y = chart_top + int(row_height * index)
        bar_y = y + int(row_height * 0.40)
        bar_height = max(8, int(row_height * 0.28))
        width = max(4, int(bar_max * min(100.0, max(0.0, value)) / 100))
        draw.rounded_rectangle(
            (chart_left, bar_y, chart_left + width, bar_y + bar_height),
            radius=4,
            fill=_rgb(preset.palette.accent if index % 2 == 0 else preset.palette.secondary),
        )
        draw.text(
            (chart_left, y),
            context[:36],
            font=label_font,
            fill=_rgb(preset.palette.text),
        )
        draw.text(
            (chart_left + width + 8, bar_y),
            f"{value:g}%",
            font=value_font,
            fill=_rgb(preset.palette.text),
        )
    note_font = _font(preset.body_font, max(10, int((y1 - y0) * 0.042)))
    draw.text(
        (x0 + margin // 2, y1 - int((y1 - y0) * 0.11)),
        "Values and labels are taken verbatim from the narration.",
        font=note_font,
        fill=_rgb(preset.palette.muted),
    )


def _draw_timeline_panel(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    box: tuple[int, int, int, int],
    margin: int,
) -> None:
    x0, y0, x1, y1 = box
    _panel(draw, box, preset.palette, alpha=245, radius=16)
    years = extract_years(visual.narration)
    labels = years if len(years) >= 2 else (visual.keywords or ["Reported", "Verified", "Open"])
    title_font = _font(preset.body_font, max(12, int((y1 - y0) * 0.055)))
    draw.text(
        (x0 + margin // 2, y0 + margin // 2),
        "TIMELINE · REPORTED SEQUENCE" if years else "REPORTED SEQUENCE",
        font=title_font,
        fill=_rgb(preset.palette.accent),
    )
    line_y = y0 + int((y1 - y0) * 0.52)
    left = x0 + margin
    right = x1 - margin
    draw.line((left, line_y, right, line_y), fill=_rgba(preset.palette.muted, 120), width=3)
    count = max(1, min(5, len(labels)))
    step = (right - left) / max(1, count - 1) if count > 1 else 0
    dot_font = _font(preset.heading_font, max(14, int((y1 - y0) * 0.08)))
    for index, label in enumerate(labels[:count]):
        x = left + step * index if count > 1 else (left + right) / 2
        draw.ellipse((x - 8, line_y - 8, x + 8, line_y + 8), fill=_rgb(preset.palette.accent))
        text_width = _text_size(draw, str(label), dot_font)[0]
        draw.text(
            (x - text_width / 2, line_y - int((y1 - y0) * 0.20)),
            str(label),
            font=dot_font,
            fill=_rgb(preset.palette.text),
        )
    note_font = _font(preset.body_font, max(10, int((y1 - y0) * 0.040)))
    draw.text(
        (x0 + margin // 2, y1 - int((y1 - y0) * 0.12)),
        "Dates shown only where they appear in the narration.",
        font=note_font,
        fill=_rgb(preset.palette.muted),
    )


def _draw_diagram_panel(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    box: tuple[int, int, int, int],
    margin: int,
) -> None:
    x0, y0, x1, y1 = box
    _panel(draw, box, preset.palette, alpha=245, radius=16)
    title_font = _font(preset.body_font, max(12, int((y1 - y0) * 0.055)))
    draw.text(
        (x0 + margin // 2, y0 + margin // 2),
        "EXPLAINER · KEY COMPONENTS",
        font=title_font,
        fill=_rgb(preset.palette.accent),
    )
    keywords = [str(item) for item in (visual.keywords or []) if str(item).strip()][:3]
    if not keywords:
        keywords = ["Context", "Evidence", "What next"]
    count = len(keywords)
    inner_top = y0 + int((y1 - y0) * 0.18)
    inner_bottom = y1 - int((y1 - y0) * 0.12)
    node_height = int((inner_bottom - inner_top) * 0.24)
    node_width = int((x1 - x0 - margin * 2) * 0.72)
    node_x = x0 + (x1 - x0 - node_width) // 2
    gap = (inner_bottom - inner_top - node_height * count) / max(1, count - 1) if count > 1 else 0
    label_font = _font(preset.body_font, max(11, int(node_height * 0.36)))
    for index, keyword in enumerate(keywords):
        node_y = inner_top + int((node_height + gap) * index)
        draw.rounded_rectangle(
            (node_x, node_y, node_x + node_width, node_y + node_height),
            radius=8,
            fill=_rgba(preset.palette.background, 235),
            outline=_rgb(preset.palette.secondary),
            width=2,
        )
        text_width = _text_size(draw, keyword[:30], label_font)[0]
        draw.text(
            (node_x + (node_width - text_width) // 2, node_y + node_height // 3),
            keyword[:30],
            font=label_font,
            fill=_rgb(preset.palette.text),
        )
        if index < count - 1:
            arrow_x = node_x + node_width // 2
            arrow_y = node_y + node_height
            draw.line(
                (arrow_x, arrow_y, arrow_x, arrow_y + int(gap)),
                fill=_rgb(preset.palette.accent),
                width=3,
            )
            draw.polygon(
                [
                    (arrow_x - 7, arrow_y + int(gap) - 2),
                    (arrow_x + 7, arrow_y + int(gap) - 2),
                    (arrow_x, arrow_y + int(gap) + 8),
                ],
                fill=_rgb(preset.palette.accent),
            )
    note_font = _font(preset.body_font, max(10, int((y1 - y0) * 0.040)))
    draw.text(
        (x0 + margin // 2, y1 - int((y1 - y0) * 0.09)),
        "Diagram reflects scene keywords; verify against the script.",
        font=note_font,
        fill=_rgb(preset.palette.muted),
    )


def _draw_quote_panel(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    box: tuple[int, int, int, int],
    margin: int,
) -> None:
    x0, y0, x1, y1 = box
    _panel(draw, box, preset.palette, alpha=245, radius=16)
    quote = " ".join((visual.narration or visual.subtitle or "").split())
    quote_font = _font(preset.heading_font, max(16, int((y1 - y0) * 0.095)))
    mark_font = _font(preset.heading_font, max(40, int((y1 - y0) * 0.30)))
    draw.text(
        (x0 + margin // 2, y0 + margin // 2),
        "“",
        font=mark_font,
        fill=_rgb(preset.palette.accent),
    )
    _draw_wrapped(
        draw,
        quote,
        (x0 + margin, y0 + int((y1 - y0) * 0.28)),
        quote_font,
        _rgb(preset.palette.text),
        x1 - x0 - margin * 2,
        max_lines=4,
    )
    attribution = visual.attribution or "Attribution pending · verify quotation"
    attr_font = _font(preset.body_font, max(11, int((y1 - y0) * 0.050)))
    draw.text(
        (x0 + margin, y1 - int((y1 - y0) * 0.18)),
        attribution[:70],
        font=attr_font,
        fill=_rgb(preset.palette.muted),
    )


def _draw_graphic_panel(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    box: tuple[int, int, int, int],
    margin: int,
    graphic_type: str,
) -> None:
    if graphic_type == "map":
        _draw_map_panel(canvas, draw, visual, preset, box, margin)
    elif graphic_type == "chart":
        _draw_chart_panel(canvas, draw, visual, preset, box, margin)
    elif graphic_type == "timeline":
        _draw_timeline_panel(canvas, draw, visual, preset, box, margin)
    elif graphic_type == "diagram":
        _draw_diagram_panel(canvas, draw, visual, preset, box, margin)
    elif graphic_type == "quote":
        _draw_quote_panel(canvas, draw, visual, preset, box, margin)
    else:
        _draw_stat_panel(canvas, draw, visual, preset, box, margin)


def _draw_split(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    visual: SceneVisual,
    preset: EditingPreset,
    width: int,
    height: int,
    margin: int,
) -> None:
    mid = width // 2
    draw.rectangle((0, 0, mid - 2, height), fill=_rgb(preset.palette.surface))
    draw.rectangle((mid + 2, 0, width, height), fill=_rgb(preset.palette.background))
    if visual.asset_path and visual.asset_path.is_file():
        try:
            with Image.open(visual.asset_path) as source:
                image = _cover_crop(source, (mid - 2, height), visual.focal_x, visual.focal_y)
            canvas.paste(image, (0, 0))
        except (OSError, ValueError):
            pass
    keywords = [str(item) for item in (visual.keywords or []) if str(item).strip()][:2]
    if len(keywords) < 2:
        keywords = ["Reported position", "Context to verify"]
    label_font = _font(preset.heading_font, max(16, int(height * 0.035)))
    body_font = _font(preset.body_font, max(12, int(height * 0.022)))
    for index, keyword in enumerate(keywords[:2]):
        x = margin if index == 0 else mid + margin
        draw.text(
            (x, margin),
            f"PANEL {chr(65 + index)}",
            font=body_font,
            fill=_rgb(preset.palette.accent),
        )
        _draw_wrapped(
            draw,
            keyword,
            (x, margin + int(height * 0.06)),
            label_font,
            _rgb(preset.palette.text),
            mid - margin * 2,
            max_lines=3,
        )
    draw.rectangle((mid - 3, 0, mid + 3, height), fill=_rgb(preset.palette.accent))


def render_scene_frame(
    visual: SceneVisual,
    preset: EditingPreset,
    width: int,
    height: int,
    output_path: Path,
    *,
    brand: str = "Newsroom",
    layout: LayoutRules | None = None,
    aspect_ratio: str = "16:9",
    show_caption: bool = True,
) -> FrameResult:
    """Render one deliberate, fully composed frame; never a blank or broken image."""

    width = max(64, int(width))
    height = max(64, int(height))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = _gradient(
        (width, height), preset.palette.background, preset.palette.surface
    ).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    active_layout = layout or (preset.landscape if width >= height else preset.portrait)
    margin = max(18, int(min(width, height) * active_layout.safe_margin_ratio))
    graphic_type = choose_graphic_type(visual, preset)
    has_asset = bool(visual.asset_path and visual.asset_path.is_file())
    photo_backdrop = False

    if graphic_type == "split":
        _draw_split(canvas, draw, visual, preset, width, height, margin)
        draw = ImageDraw.Draw(canvas)
    elif graphic_type == "photo" and has_asset:
        if not _draw_photo_frame(
            canvas, visual, preset, width, height, margin, aspect_ratio=aspect_ratio
        ):
            graphic_type = "typography"
            draw = ImageDraw.Draw(canvas)
        else:
            photo_backdrop = True
    else:
        if has_asset and graphic_type in {"photo", "typography"}:
            if not _draw_photo_frame(
                canvas, visual, preset, width, height, margin, aspect_ratio=aspect_ratio
            ):
                has_asset = False
            else:
                photo_backdrop = True
            draw = ImageDraw.Draw(canvas)

    brand_font = _font(preset.body_font, max(10, int(height * 0.016)))
    brand_width = _text_size(draw, brand.upper(), brand_font)[0]
    # The brand mark sits in the top-right corner; keep the kicker clear of it.
    kicker_reserve = brand_width + margin

    if graphic_type in {"map", "chart", "timeline", "diagram", "stat", "quote"}:
        if width >= height:
            panel_box = (
                int(width * 0.47),
                int(height * 0.18),
                width - margin,
                int(height * 0.72),
            )
            headline_width = int(width * 0.40)
        else:
            panel_box = (
                margin,
                int(height * 0.44),
                width - margin,
                int(height * 0.66),
            )
            headline_width = width - margin * 2
        graphic_box = panel_box
        inset_band = 0
        if has_asset and preset.key in {"vox", "documentary", "magazine", "photojournalism"}:
            # Reserve the bottom band of the card for the evidence photo and
            # draw the graphic in the space above it. Painting the inset first
            # simply hid it behind the panel background.
            inset_band = max(70, int((panel_box[3] - panel_box[1]) * 0.32))
            graphic_box = (panel_box[0], panel_box[1], panel_box[2], panel_box[3] - inset_band)
        _draw_graphic_panel(canvas, draw, visual, preset, graphic_box, margin, graphic_type)
        draw = ImageDraw.Draw(canvas)
        if inset_band:
            try:
                with Image.open(visual.asset_path) as source:  # type: ignore[arg-type]
                    inset = _cover_crop(
                        source,
                        (
                            max(80, panel_box[2] - panel_box[0] - margin),
                            max(60, inset_band - margin // 2),
                        ),
                        visual.focal_x,
                        visual.focal_y,
                    )
                inset_box = (
                    panel_box[0] + margin // 2,
                    panel_box[3] - inset.height - margin // 2,
                    panel_box[0] + margin // 2 + inset.width,
                    panel_box[3] - margin // 2,
                )
                _paste_rounded(canvas, inset, inset_box, 8)
                draw = ImageDraw.Draw(canvas)
            except (OSError, ValueError):
                pass
        _draw_headline(
            draw,
            visual,
            preset,
            active_layout,
            width,
            height,
            margin,
            max_width=headline_width,
            max_lines=4,
            on_photo=photo_backdrop,
            kicker_reserve=kicker_reserve,
        )
    elif graphic_type == "split":
        _draw_headline(
            draw,
            visual,
            preset,
            active_layout,
            width,
            height,
            margin,
            max_width=width - margin * 2,
            max_lines=2,
            on_photo=photo_backdrop,
            kicker_reserve=kicker_reserve,
        )
    else:
        _draw_headline(
            draw,
            visual,
            preset,
            active_layout,
            width,
            height,
            margin,
            on_photo=photo_backdrop,
            kicker_reserve=kicker_reserve,
        )

    if show_caption and not _caption_repeats_headline(visual, preset, active_layout):
        _draw_caption(
            draw, visual, preset, width, height, margin, aspect_ratio=aspect_ratio
        )
    _draw_lower_third(draw, visual, preset, width, height, margin)
    _draw_ticker(draw, visual, preset, width, height, margin)
    source_text = _draw_source(
        draw,
        visual,
        preset,
        width,
        height,
        margin,
        max(11, int(height * 0.017)),
        fallback_note=(
            None
            if has_asset
            else "Designed graphic · no external asset"
        ),
    )
    draw.text(
        (width - margin - brand_width, margin),
        brand.upper(),
        font=brand_font,
        fill=(232, 232, 232) if photo_backdrop else _rgb(preset.palette.muted),
    )
    if preset.grain:
        canvas = _add_grain(canvas, preset.grain)
    canvas.convert("RGB").save(output_path, "JPEG", quality=92, optimize=True)
    return FrameResult(
        path=output_path,
        graphic_type=graphic_type,
        has_asset=has_asset,
        headline=_headline_for(visual, preset, active_layout),
        source_text=source_text,
    )


def _add_grain(canvas: Image.Image, amount: float) -> Image.Image:
    noise = Image.effect_noise(canvas.size, sigma=max(1, int(amount * 90))).convert("L")
    noise = noise.filter(ImageFilter.GaussianBlur(radius=0.4))
    grain = Image.merge(
        "RGBA", (noise, noise, noise, Image.new("L", canvas.size, int(amount * 60)))
    )
    canvas.alpha_composite(grain)
    return canvas


def contact_sheet(paths: Iterable[Path], output_path: Path, columns: int = 3) -> Path:
    """Create a deterministic review sheet for rendered scene frames."""

    images = [Image.open(path).convert("RGB") for path in paths if path.is_file()]
    if not images:
        raise ValueError("No frames available for a contact sheet")
    thumb_width = 480
    thumb_height = max(1, round(images[0].height * thumb_width / images[0].width))
    rows = math.ceil(len(images) / columns)
    sheet = Image.new("RGB", (columns * thumb_width, rows * thumb_height), (8, 12, 20))
    for index, image in enumerate(images):
        thumb = image.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
        sheet.paste(thumb, ((index % columns) * thumb_width, (index // columns) * thumb_height))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, "JPEG", quality=90)
    for image in images:
        image.close()
    return output_path
