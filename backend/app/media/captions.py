from __future__ import annotations

import html
import math
import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_CAPTION_PRESET = "clean_editorial"

CAPTION_STATUSES = (
    "not_generated",
    "generating",
    "ready",
    "edited",
    "rendering",
    "completed",
    "failed",
)

SOURCE_LABELS = {
    "transcription": "Transcribed from the final voiceover",
    "narration_alignment": "Narration-aligned (timing from the TTS audio; not speech-verified)",
    "script_fallback": "Script fallback (not speech-verified)",
    "imported": "Imported subtitle file",
}

VERTICAL_POSITIONS = ("bottom", "center", "top")
ALIGNMENTS = ("left", "center", "right")
FONT_WEIGHTS = ("regular", "medium", "semibold", "bold")

# Conservative limits. These are heuristics, not universal broadcast rules.
MIN_FONT_SIZE = 20
MAX_FONT_SIZE = 120
MIN_CUE_SECONDS = 0.55
MAX_CUE_SECONDS = 10.0
DEFAULT_MAX_CPS = 22.0
HOLD_SECONDS = 0.14
MIN_GAP_SECONDS = 0.02


@dataclass(frozen=True)
class AspectLayout:
    """Aspect-specific geometry, measured against the real output frame."""

    font_size: int = 46
    max_chars_per_line: int = 42
    max_words_per_line: int = 9
    max_lines: int = 2
    vertical_position: str = "bottom"
    safe_bottom_ratio: float = 0.08
    safe_margin_ratio: float = 0.06
    line_spacing: float = 1.18

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None, fallback: AspectLayout) -> AspectLayout:
        if not isinstance(value, dict):
            return fallback
        data = asdict(fallback)
        for key in data:
            if key in value and value[key] is not None:
                data[key] = value[key]
        return cls(**data)


@dataclass(frozen=True)
class CaptionSettings:
    """Typography, colour, placement, and readability controls for one track."""

    preset_key: str = DEFAULT_CAPTION_PRESET
    font_family: str = "Segoe UI"
    font_weight: str = "semibold"
    text_color: str = "#FFFFFF"
    highlight_color: str = "#9DB2E6"
    background_opacity: float = 0.45
    background_padding: int = 12
    outline_width: int = 2
    shadow: int = 1
    alignment: str = "center"
    timing_offset_ms: int = 0
    speaker_labels: bool = False
    word_highlight: bool = False
    emphasis: bool = False
    uppercase: bool = False
    fade_ms: int = 0
    landscape: AspectLayout = field(default_factory=AspectLayout)
    portrait: AspectLayout = field(
        default_factory=lambda: AspectLayout(
            font_size=58,
            max_chars_per_line=26,
            max_words_per_line=7,
            max_lines=3,
            vertical_position="bottom",
            safe_bottom_ratio=0.18,
            safe_margin_ratio=0.07,
        )
    )

    def layout_for(self, aspect_ratio: str) -> AspectLayout:
        return self.portrait if aspect_ratio == "9:16" else self.landscape

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["landscape"] = self.landscape.to_dict()
        payload["portrait"] = self.portrait.to_dict()
        return payload

    @classmethod
    def from_dict(
        cls, value: dict[str, Any] | None, fallback: CaptionSettings | None = None
    ) -> CaptionSettings:
        base = fallback or preset_settings(DEFAULT_CAPTION_PRESET)
        if not isinstance(value, dict):
            return base
        defaults = base.to_dict()
        for key, item in value.items():
            if key in {"landscape", "portrait"}:
                continue
            if key in defaults and item is not None:
                defaults[key] = item
        landscape = AspectLayout.from_dict(value.get("landscape"), base.landscape)
        portrait = AspectLayout.from_dict(value.get("portrait"), base.portrait)
        return cls(
            **{key: defaults[key] for key in defaults if key not in {"landscape", "portrait"}},
            landscape=landscape,
            portrait=portrait,
        )

    def with_updates(self, updates: dict[str, Any]) -> CaptionSettings:
        return CaptionSettings.from_dict(updates, fallback=self)


@dataclass(frozen=True)
class CaptionPreset:
    key: str
    name: str
    description: str
    best_for: str
    settings: CaptionSettings

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "best_for": self.best_for,
            "settings": self.settings.to_dict(),
        }


def _landscape(
    font_size: int,
    max_chars: int,
    max_words: int,
    max_lines: int,
    *,
    safe_bottom: float = 0.08,
    safe_margin: float = 0.06,
    vertical: str = "bottom",
) -> AspectLayout:
    return AspectLayout(
        font_size=font_size,
        max_chars_per_line=max_chars,
        max_words_per_line=max_words,
        max_lines=max_lines,
        vertical_position=vertical,
        safe_bottom_ratio=safe_bottom,
        safe_margin_ratio=safe_margin,
    )


def _portrait(
    font_size: int,
    max_chars: int,
    max_words: int,
    max_lines: int,
    *,
    safe_bottom: float = 0.18,
    safe_margin: float = 0.07,
    vertical: str = "bottom",
) -> AspectLayout:
    return AspectLayout(
        font_size=font_size,
        max_chars_per_line=max_chars,
        max_words_per_line=max_words,
        max_lines=max_lines,
        vertical_position=vertical,
        safe_bottom_ratio=safe_bottom,
        safe_margin_ratio=safe_margin,
    )


CAPTION_PRESETS: dict[str, CaptionPreset] = {
    "clean_editorial": CaptionPreset(
        key="clean_editorial",
        name="Clean Editorial",
        description="Quiet white captions with a soft box and a restrained blue highlight.",
        best_for="General newsroom output and the default review look",
        settings=CaptionSettings(
            preset_key="clean_editorial",
            font_family="Segoe UI",
            font_weight="semibold",
            text_color="#FFFFFF",
            highlight_color="#9DB2E6",
            background_opacity=0.45,
            background_padding=12,
            outline_width=2,
            shadow=1,
            alignment="center",
            landscape=_landscape(46, 42, 9, 2),
            portrait=_portrait(58, 26, 7, 3),
        ),
    ),
    "professional_news": CaptionPreset(
        key="professional_news",
        name="Professional News",
        description="Broadcast-style lower-third captions with strong contrast and no decoration.",
        best_for="Desktop, television, and formal news reports",
        settings=CaptionSettings(
            preset_key="professional_news",
            font_family="Arial",
            font_weight="bold",
            text_color="#FFFFFF",
            highlight_color="#8FB3D9",
            background_opacity=0.86,
            background_padding=14,
            outline_width=1,
            shadow=0,
            alignment="left",
            landscape=_landscape(44, 46, 10, 2, safe_bottom=0.09),
            portrait=_portrait(56, 28, 8, 3),
        ),
    ),
    "documentary": CaptionPreset(
        key="documentary",
        name="Documentary",
        description="Understated serif captions with generous spacing and no background panel.",
        best_for="Long-form context, interviews, and observational footage",
        settings=CaptionSettings(
            preset_key="documentary",
            font_family="Georgia",
            font_weight="medium",
            text_color="#F5F1E8",
            highlight_color="#D9B878",
            background_opacity=0.0,
            background_padding=0,
            outline_width=2,
            shadow=2,
            alignment="center",
            landscape=_landscape(44, 40, 8, 2, safe_bottom=0.10),
            portrait=_portrait(56, 26, 7, 3, safe_bottom=0.19),
        ),
    ),
    "social_media": CaptionPreset(
        key="social_media",
        name="Social Media",
        description="Large, high-contrast captions sized for mobile viewing and platform UI.",
        best_for="9:16 vertical video and short social cuts",
        settings=CaptionSettings(
            preset_key="social_media",
            font_family="Arial",
            font_weight="bold",
            text_color="#FFFFFF",
            highlight_color="#FACC15",
            background_opacity=0.62,
            background_padding=16,
            outline_width=2,
            shadow=2,
            alignment="center",
            landscape=_landscape(52, 34, 7, 2, safe_bottom=0.10),
            portrait=_portrait(68, 22, 5, 3, safe_bottom=0.22, safe_margin=0.08),
        ),
    ),
    "kinetic": CaptionPreset(
        key="kinetic",
        name="Kinetic",
        description=(
            "Restrained word-by-word highlighting without bouncing, flashing, "
            "or karaoke colour changes."
        ),
        best_for="Social edits that still need a calm, editorial feel",
        settings=CaptionSettings(
            preset_key="kinetic",
            font_family="Segoe UI",
            font_weight="bold",
            text_color="#FFFFFF",
            highlight_color="#9DB2E6",
            background_opacity=0.38,
            background_padding=10,
            outline_width=2,
            shadow=1,
            alignment="center",
            word_highlight=True,
            landscape=_landscape(50, 30, 6, 2, safe_bottom=0.10),
            portrait=_portrait(64, 22, 5, 3, safe_bottom=0.20),
        ),
    ),
    "emphasis": CaptionPreset(
        key="emphasis",
        name="Emphasis",
        description=(
            "Selective colour on names, numbers, and key phrases; the rest of "
            "the sentence stays quiet."
        ),
        best_for="Data-led stories and explainers",
        settings=CaptionSettings(
            preset_key="emphasis",
            font_family="Arial",
            font_weight="semibold",
            text_color="#FFFFFF",
            highlight_color="#D9B878",
            background_opacity=0.50,
            background_padding=12,
            outline_width=2,
            shadow=1,
            alignment="center",
            emphasis=True,
            landscape=_landscape(48, 36, 8, 2, safe_bottom=0.09),
            portrait=_portrait(60, 24, 6, 3, safe_bottom=0.20),
        ),
    ),
}


def preset_settings(key: str | None) -> CaptionSettings:
    preset = CAPTION_PRESETS.get(key or DEFAULT_CAPTION_PRESET)
    return (preset or CAPTION_PRESETS[DEFAULT_CAPTION_PRESET]).settings


def list_caption_presets() -> list[dict[str, Any]]:
    return [preset.to_public_dict() for preset in CAPTION_PRESETS.values()]


@dataclass
class CaptionWord:
    text: str
    start: float
    end: float
    emphasis: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start": round(max(0.0, self.start), 3),
            "end": round(max(0.0, self.end), 3),
            "emphasis": bool(self.emphasis),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CaptionWord:
        return cls(
            text=str(value.get("text", "")),
            start=float(value.get("start", 0.0) or 0.0),
            end=float(value.get("end", 0.0) or 0.0),
            emphasis=bool(value.get("emphasis", False)),
        )


@dataclass
class CaptionCue:
    index: int
    start: float
    end: float
    text: str
    words: list[CaptionWord] = field(default_factory=list)
    speaker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": round(max(0.0, self.start), 3),
            "end": round(max(0.0, self.end), 3),
            "text": self.text,
            "words": [word.to_dict() for word in self.words],
            "speaker": self.speaker,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CaptionCue:
        words = [
            CaptionWord.from_dict(item)
            for item in (value.get("words") or [])
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
        return cls(
            index=int(value.get("index", 0) or 0),
            start=float(value.get("start", 0.0) or 0.0),
            end=float(value.get("end", 0.0) or 0.0),
            text=" ".join(str(value.get("text", "")).split()),
            words=words,
            speaker=(str(value.get("speaker")).strip() or None)
            if value.get("speaker") is not None
            else None,
        )


@dataclass
class CaptionCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class CaptionGeneration:
    cues: list[CaptionCue]
    source: str
    source_detail: str
    language: str
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- text


ABBREVIATION = re.compile(
    r"\b(?:U\.S|U\.K|E\.U|U\.N|Mr|Mrs|Ms|Dr|Sen|Rep|Gov|Gen|Lt|Col|Sgt|Jr|Sr|"
    r"vs|etc|Inc|Ltd|Co|No|St|a\.m|p\.m)\.",
    re.IGNORECASE,
)
SENTENCE_END = re.compile(r"[.!?\u0964]$")
CLAUSE_END = re.compile(r"[,;:\u2013\u2014]$")
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
LATIN_LETTER = re.compile(r"[A-Za-z]")
HINGLISH_WORDS = re.compile(
    r"\b(?:hai|hain|nahi|nahin|kya|aap|hum|yeh|ye|woh|wo|kar|raha|rahi|"
    r"ke|ka|ki|mein|se|aur|par|bhi|ko|tha|thi)\b",
    re.IGNORECASE,
)
EMPHASIS_PATTERN = re.compile(
    r"(?:\b\d[\d,.]*(?:%|bn|million|billion|crore|lakh|km|kg)?\b|"
    r"\b(?:Rs|USD|INR|EUR)\b|[\u20B9$€£]|\"[^\"]+\"|\u201c[^\u201d]+\u201d)"
)
PROPER_NOUN = re.compile(r"^[A-Z][A-Za-z'\u2019-]+$")


def normalise_caption_text(text: str) -> str:
    value = unicodedata.normalize("NFC", text or "")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\s*\n\s*", " ", value)
    return value.strip()


def detect_language(text: str, fallback: str = "en") -> str:
    value = normalise_caption_text(text)
    if not value:
        return fallback
    devanagari = len(DEVANAGARI.findall(value))
    latin = len(LATIN_LETTER.findall(value))
    if devanagari and devanagari >= max(2, latin * 0.15):
        return "hi"
    # One borrowed word is common in English copy; two or more markers indicate
    # romanised Hindi/Hinglish. This keeps ordinary English out of the
    # Devanagari font path.
    if latin and len({match.lower() for match in HINGLISH_WORDS.findall(value)}) >= 2:
        return "hi-en"
    return "en"


def split_sentences(text: str) -> list[str]:
    guarded = ABBREVIATION.sub(lambda match: match.group(0).replace(".", "\u2024"), text)
    parts = re.split(r"(?<=[.!?\u0964])\s+", guarded)
    return [part.replace("\u2024", ".").strip() for part in parts if part.strip()]


def _word_weight(word: str) -> float:
    stripped = re.sub(r"[^\w\u0900-\u097F]", "", word)
    weight = max(1.0, float(len(stripped))) + 1.0
    if SENTENCE_END.search(word):
        weight += 2.2
    elif CLAUSE_END.search(word):
        weight += 1.1
    return weight


def _is_emphasis(word: str, index: int, words: list[str]) -> bool:
    token = word.strip()
    if not token:
        return False
    if EMPHASIS_PATTERN.search(token):
        return True
    if token.isupper() and len(token) >= 3:
        return True
    if PROPER_NOUN.match(token) and index > 0:
        previous = words[index - 1].strip()
        if previous and previous[0].islower() and previous not in {"The", "A", "An"}:
            return False
        # A capitalised word mid-sentence is usually a name or place.
        return index > 0
    return False


def allocate_words(
    text: str,
    start: float,
    duration: float,
    *,
    emphasis: bool = False,
) -> list[CaptionWord]:
    tokens = normalise_caption_text(text).split()
    if not tokens:
        return []
    weights = [_word_weight(token) for token in tokens]
    total = sum(weights) or 1.0
    words: list[CaptionWord] = []
    elapsed = 0.0
    for index, token in enumerate(tokens):
        share = duration * (weights[index] / total)
        word_start = start + elapsed
        word_end = start + elapsed + share
        words.append(
            CaptionWord(
                text=token,
                start=word_start,
                end=word_end,
                emphasis=_is_emphasis(token, index, tokens) if emphasis else False,
            )
        )
        elapsed += share
    if words:
        words[-1].end = max(words[-1].end, start + duration)
    return words


# --------------------------------------------------------------------- segmentation


def _wrap_tokens(tokens: list[CaptionWord], max_chars: int) -> list[list[CaptionWord]]:
    lines: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    length = 0
    for token in tokens:
        addition = len(token.text) + (1 if current else 0)
        if current and length + addition > max_chars:
            lines.append(current)
            current = [token]
            length = len(token.text)
        else:
            current.append(token)
            length += addition
    if current:
        lines.append(current)
    return lines


def _fits(tokens: list[CaptionWord], layout: AspectLayout) -> bool:
    if not tokens:
        return True
    if len(tokens) > layout.max_words_per_line * layout.max_lines:
        return False
    return len(_wrap_tokens(tokens, layout.max_chars_per_line)) <= layout.max_lines


def _can_keep_proper_pair(previous: CaptionWord, token: CaptionWord) -> bool:
    if not previous.text or not token.text:
        return False
    if SENTENCE_END.search(previous.text) or CLAUSE_END.search(previous.text):
        return False
    return (
        previous.text[0].isupper()
        and token.text[0].isupper()
        and len(previous.text) > 1
        and len(token.text) > 1
    )


def _group_tokens(tokens: list[CaptionWord], layout: AspectLayout) -> list[list[CaptionWord]]:
    groups: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    for token in tokens:
        if not current:
            current = [token]
            continue
        candidate = [*current, token]
        if _fits(candidate, layout):
            current = candidate
            if SENTENCE_END.search(token.text) and len(" ".join(w.text for w in current)) >= 18:
                groups.append(current)
                current = []
            continue
        # Prefer a clause boundary when a cue has to break.
        split_at = None
        for index in range(len(current) - 1, 0, -1):
            if CLAUSE_END.search(current[index - 1].text):
                split_at = index
                break
        if split_at and split_at < len(current):
            groups.append(current[:split_at])
            current = current[split_at:]
            if _fits([*current, token], layout):
                current = [*current, token]
            else:
                groups.append(current)
                current = [token]
            continue
        # Do not strand the second half of a proper-noun pair on the next cue.
        if len(current) > 1 and _can_keep_proper_pair(current[-1], token):
            moved = current.pop()
            groups.append(current)
            current = [moved, token]
            continue
        groups.append(current)
        current = [token]
    if current:
        groups.append(current)
    return groups


def _merge_short_groups(
    groups: list[list[CaptionWord]], layout: AspectLayout
) -> list[list[CaptionWord]]:
    merged: list[list[CaptionWord]] = []
    for group in groups:
        if (
            merged
            and len(" ".join(word.text for word in group)) < 14
            and _fits([*merged[-1], *group], layout)
        ):
            merged[-1] = [*merged[-1], *group]
        else:
            merged.append(group)
    return merged


def _rebalance_short_groups(
    groups: list[list[CaptionWord]], layout: AspectLayout
) -> list[list[CaptionWord]]:
    """Avoid a dangling one-word cue by borrowing words from the previous cue."""

    if len(groups) < 2:
        return groups
    for index in range(1, len(groups)):
        current = groups[index]
        previous = groups[index - 1]
        if len(_group_text(current)) >= 18 or len(previous) <= 1:
            continue
        target = min(layout.max_chars_per_line, 24)
        moved: list[CaptionWord] = []
        original = list(current)
        while len(previous) > 1 and len(_group_text([*moved, *original])) < target:
            moved.insert(0, previous.pop())
        current = [*moved, *original]
        if moved and _fits(previous, layout) and _fits(current, layout):
            groups[index - 1] = previous
            groups[index] = current
    return groups


def _group_text(group: list[CaptionWord]) -> str:
    return " ".join(word.text for word in group).strip()


def _cues_from_words(
    words: list[CaptionWord],
    layout: AspectLayout,
    *,
    start_index: int = 1,
    speaker: str | None = None,
) -> list[CaptionCue]:
    if not words:
        return []
    groups = _merge_short_groups(_group_tokens(words, layout), layout)
    groups = _rebalance_short_groups(groups, layout)
    cues: list[CaptionCue] = []
    for group in groups:
        text = _group_text(group)
        if not text:
            continue
        cues.append(
            CaptionCue(
                index=start_index + len(cues),
                start=group[0].start,
                end=group[-1].end,
                text=text,
                words=group,
                speaker=speaker,
            )
        )
    return cues


def _clamp_cue_timing(
    cues: list[CaptionCue],
    *,
    duration: float,
    hold: float = HOLD_SECONDS,
    min_cue: float = MIN_CUE_SECONDS,
    gap: float = MIN_GAP_SECONDS,
) -> list[CaptionCue]:
    if not cues:
        return []
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    clamped: list[CaptionCue] = []
    for index, cue in enumerate(ordered):
        start = max(0.0, min(cue.start, duration))
        end = max(start, min(cue.end, duration))
        next_start = (
            max(start, min(ordered[index + 1].start, duration))
            if index + 1 < len(ordered)
            else duration
        )
        end = min(end + hold, next_start - gap if index + 1 < len(ordered) else duration)
        if end - start < min_cue:
            end = min(start + min_cue, next_start - gap if index + 1 < len(ordered) else duration)
        if end <= start:
            end = min(duration, start + 0.12)
        clamped.append(
            CaptionCue(
                index=index + 1,
                start=round(start, 3),
                end=round(max(start + 0.05, end), 3),
                text=cue.text,
                words=cue.words,
                speaker=cue.speaker,
            )
        )
    return clamped


def apply_timing_offset(
    cues: list[CaptionCue], offset_ms: int, duration: float
) -> list[CaptionCue]:
    if not offset_ms:
        return _clamp_cue_timing(cues, duration=duration)
    offset = offset_ms / 1000.0
    shifted = [
        CaptionCue(
            index=cue.index,
            start=max(0.0, cue.start + offset),
            end=max(0.0, cue.end + offset),
            text=cue.text,
            words=[
                replace(word, start=max(0.0, word.start + offset), end=max(0.0, word.end + offset))
                for word in cue.words
            ],
            speaker=cue.speaker,
        )
        for cue in cues
    ]
    return _clamp_cue_timing(shifted, duration=duration)


def generate_from_chunks(
    chunks: Iterable[Any],
    settings: CaptionSettings,
    *,
    duration: float,
    language: str | None = None,
    source: str = "narration_alignment",
    source_detail: str = "",
) -> CaptionGeneration:
    layout = settings.layout_for("16:9")
    all_cues: list[CaptionCue] = []
    text_probe: list[str] = []
    warnings: list[str] = []
    for chunk in chunks:
        text = normalise_caption_text(getattr(chunk, "text", "") or "")
        if not text:
            continue
        text_probe.append(text)
        chunk_start = max(0.0, float(getattr(chunk, "start_seconds", 0.0) or 0.0))
        chunk_duration = max(0.05, float(getattr(chunk, "duration_seconds", 0.0) or 0.0))
        words = allocate_words(
            text,
            chunk_start,
            min(chunk_duration, max(0.05, duration - chunk_start)),
            emphasis=settings.emphasis,
        )
        # The final cue set is laid out per aspect. Chunk generation uses the
        # landscape limit; the cue text is re-wrapped for portrait at render time.
        cues = _cues_from_words(words, layout, start_index=len(all_cues) + 1)
        all_cues.extend(cues)
    if not all_cues:
        return CaptionGeneration(
            cues=[],
            source=source,
            source_detail=source_detail,
            language=language or "en",
            warnings=["No narration text was available for caption generation."],
        )
    detected = language or detect_language(" ".join(text_probe))
    all_cues = apply_timing_offset(all_cues, settings.timing_offset_ms, duration)
    all_cues = _retime_for_aspect(all_cues, settings, duration)
    if all_cues and all_cues[-1].end < duration - 2.0:
        warnings.append(
            "Captions end more than two seconds before the narration. Check the final chunk timing."
        )
    return CaptionGeneration(
        cues=all_cues,
        source=source,
        source_detail=source_detail or SOURCE_LABELS.get(source, source),
        language=detected,
        warnings=warnings,
    )


def generate_from_transcription(
    segments: Iterable[dict[str, Any]],
    settings: CaptionSettings,
    *,
    duration: float,
    language: str | None = None,
    source_detail: str = "Speech recognition",
) -> CaptionGeneration:
    """Build cues from ASR segments that may include word timestamps."""

    layout = settings.layout_for("16:9")
    words: list[CaptionWord] = []
    text_probe: list[str] = []
    for segment in segments:
        text = normalise_caption_text(str(segment.get("text", "")))
        if not text:
            continue
        text_probe.append(text)
        start = max(0.0, float(segment.get("start", 0.0) or 0.0))
        end = max(start + 0.05, float(segment.get("end", start + 0.5) or start + 0.5))
        raw_words = segment.get("words") or []
        if raw_words:
            for item in raw_words:
                token = normalise_caption_text(str(item.get("word", item.get("text", ""))))
                if not token:
                    continue
                word_start = max(start, float(item.get("start", start) or start))
                fallback_end = word_start + 0.1
                word_end = max(
                    word_start + 0.02,
                    float(item.get("end", fallback_end) or fallback_end),
                )
                words.append(
                    CaptionWord(
                        text=token,
                        start=word_start,
                        end=word_end,
                        emphasis=_is_emphasis(token, len(words), text.split())
                        if settings.emphasis
                        else False,
                    )
                )
        else:
            words.extend(
                allocate_words(
                    text,
                    start,
                    end - start,
                    emphasis=settings.emphasis,
                )
            )
    cues = _cues_from_words(words, layout)
    cues = apply_timing_offset(cues, settings.timing_offset_ms, duration)
    cues = _retime_for_aspect(cues, settings, duration)
    detected = language or detect_language(" ".join(text_probe))
    return CaptionGeneration(
        cues=cues,
        source="transcription",
        source_detail=source_detail,
        language=detected,
        warnings=[] if cues else ["The transcription produced no usable captions."],
    )


def generate_from_script(
    text: str,
    settings: CaptionSettings,
    *,
    duration: float,
    language: str | None = None,
) -> CaptionGeneration:
    clean = normalise_caption_text(text)
    words = allocate_words(clean, 0.0, max(0.5, duration), emphasis=settings.emphasis)
    cues = _cues_from_words(words, settings.layout_for("16:9"))
    cues = apply_timing_offset(cues, settings.timing_offset_ms, duration)
    cues = _retime_for_aspect(cues, settings, duration)
    return CaptionGeneration(
        cues=cues,
        source="script_fallback",
        source_detail=SOURCE_LABELS["script_fallback"],
        language=language or detect_language(clean),
        warnings=[
            "Captions were built from the script because no narration timing was available. "
            "They are not verified against the spoken audio."
        ],
    )


def _retime_for_aspect(
    cues: list[CaptionCue], settings: CaptionSettings, duration: float
) -> list[CaptionCue]:
    """Keep cue timing while making sure portrait limits can wrap the text."""

    if not cues:
        return []
    landscape = settings.layout_for("16:9")
    portrait = settings.layout_for("9:16")
    if (
        landscape.max_chars_per_line == portrait.max_chars_per_line
        and landscape.max_lines == portrait.max_lines
        and landscape.max_words_per_line == portrait.max_words_per_line
    ):
        return cues
    # Cues are stored once. If the portrait layout is tighter, split any cue
    # whose text would overflow it, preserving the word-level timing.
    output: list[CaptionCue] = []
    for cue in cues:
        if _fits(cue.words, portrait) or not cue.words:
            output.append(cue)
            continue
        subgroups = _merge_short_groups(_group_tokens(cue.words, portrait), portrait)
        for subgroup in subgroups:
            output.append(
                CaptionCue(
                    index=len(output) + 1,
                    start=subgroup[0].start,
                    end=subgroup[-1].end,
                    text=_group_text(subgroup),
                    words=subgroup,
                    speaker=cue.speaker,
                )
            )
    return _clamp_cue_timing(output, duration=duration)


# ------------------------------------------------------------------------- wrapping


def wrap_caption_text(text: str, max_chars: int, max_lines: int) -> list[str]:
    words = normalise_caption_text(text).split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return lines
    # Keep every word: move overflow into the final allowed line and let the
    # validator flag it rather than silently dropping speech.
    head = lines[: max_lines - 1]
    tail = " ".join(lines[max_lines - 1 :])
    return [*head, tail]


def _wrap_word_indices(words: list[CaptionWord], max_chars: int) -> list[list[int]]:
    lines: list[list[int]] = []
    current: list[int] = []
    length = 0
    for index, word in enumerate(words):
        addition = len(word.text) + (1 if current else 0)
        if current and length + addition > max_chars:
            lines.append(current)
            current = [index]
            length = len(word.text)
        else:
            current.append(index)
            length += addition
    if current:
        lines.append(current)
    return lines


# ------------------------------------------------------------------------- validation


def validate_settings(
    settings: CaptionSettings,
    *,
    width: int,
    height: int,
    aspect_ratio: str,
) -> list[CaptionCheck]:
    checks: list[CaptionCheck] = []
    layout = settings.layout_for(aspect_ratio)
    if settings.font_weight not in FONT_WEIGHTS:
        checks.append(CaptionCheck("font_weight", "failed", "Choose a supported font weight."))
    if settings.alignment not in ALIGNMENTS:
        checks.append(
            CaptionCheck("alignment", "failed", "Choose left, center, or right alignment.")
        )
    if layout.vertical_position not in VERTICAL_POSITIONS:
        checks.append(
            CaptionCheck("vertical_position", "failed", "Choose bottom, center, or top placement.")
        )
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", settings.text_color or ""):
        checks.append(
            CaptionCheck("text_color", "failed", "Text colour must be a six-digit hex value.")
        )
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", settings.highlight_color or ""):
        checks.append(
            CaptionCheck(
                "highlight_color",
                "failed",
                "Highlight colour must be a six-digit hex value.",
            )
        )
    if not (MIN_FONT_SIZE <= layout.font_size <= MAX_FONT_SIZE):
        checks.append(
            CaptionCheck(
                "font_size",
                "failed",
                f"Font size must be between {MIN_FONT_SIZE} and {MAX_FONT_SIZE} reference pixels.",
            )
        )
    if not (10 <= layout.max_chars_per_line <= 80):
        checks.append(
            CaptionCheck(
                "max_chars_per_line",
                "failed",
                "Characters per line must be between 10 and 80.",
            )
        )
    if not (2 <= layout.max_words_per_line <= 20):
        checks.append(
            CaptionCheck(
                "max_words_per_line",
                "failed",
                "Words per line must be between 2 and 20.",
            )
        )
    if not (1 <= layout.max_lines <= 4):
        checks.append(CaptionCheck("max_lines", "failed", "Visible lines must be between 1 and 4."))
    if not (0.0 <= settings.background_opacity <= 1.0):
        checks.append(
            CaptionCheck(
                "background_opacity",
                "failed",
                "Background opacity must be between 0 and 100 percent.",
            )
        )
    if not (0 <= settings.background_padding <= 48):
        checks.append(
            CaptionCheck("background_padding", "failed", "Background padding must be 0–48 px.")
        )
    if not (0 <= settings.outline_width <= 10):
        checks.append(CaptionCheck("outline_width", "failed", "Outline must be 0–10 px."))
    if not (0 <= settings.shadow <= 10):
        checks.append(CaptionCheck("shadow", "failed", "Shadow must be 0–10 px."))
    if not (-5_000 <= settings.timing_offset_ms <= 5_000):
        checks.append(
            CaptionCheck("timing_offset", "failed", "Timing offset must be within ±5 seconds.")
        )
    if not (0.0 <= layout.safe_margin_ratio <= 0.30):
        checks.append(
            CaptionCheck("safe_margin", "failed", "Safe margin must be 0–30% of the frame.")
        )
    if not (0.0 <= layout.safe_bottom_ratio <= 0.40):
        checks.append(
            CaptionCheck("safe_bottom", "failed", "Bottom safe area must be 0–40% of the frame.")
        )

    font_size = _scaled_font_size(layout.font_size, height, aspect_ratio)
    margin_x = int(width * layout.safe_margin_ratio)
    usable_width = max(1, width - margin_x * 2)
    estimated_width = int(
        layout.max_chars_per_line * font_size * (0.62 if settings.language_is_devanagari else 0.56)
    )
    if estimated_width > usable_width:
        checks.append(
            CaptionCheck(
                "frame_width",
                "failed",
                (
                    f"{layout.max_chars_per_line} characters at {font_size}px need about "
                    f"{estimated_width}px but only {usable_width}px is inside the safe area."
                ),
            )
        )
    else:
        checks.append(
            CaptionCheck(
                "frame_width",
                "passed",
                f"Line width fits inside the {usable_width}px safe area.",
            )
        )

    box_height = int(
        layout.max_lines * font_size * layout.line_spacing
        + settings.background_padding * (height / 1080) * 2
    )
    usable_height = int(height * (1.0 - layout.safe_bottom_ratio - layout.safe_margin_ratio))
    if box_height > usable_height:
        checks.append(
            CaptionCheck(
                "frame_height",
                "failed",
                (
                    f"{layout.max_lines} lines need about {box_height}px but only "
                    f"{usable_height}px is inside the safe area."
                ),
            )
        )
    else:
        checks.append(
            CaptionCheck(
                "frame_height",
                "passed",
                f"{layout.max_lines} lines fit inside the {usable_height}px safe area.",
            )
        )
    if aspect_ratio == "9:16" and layout.safe_bottom_ratio < 0.12:
        checks.append(
            CaptionCheck(
                "platform_safe_area",
                "warning",
                "Vertical captions below 12% from the bottom may sit under platform UI.",
            )
        )
    else:
        checks.append(
            CaptionCheck(
                "platform_safe_area",
                "passed",
                "Captions stay inside the platform-safe area for this format.",
            )
        )
    if settings.background_opacity < 0.18 and settings.outline_width < 1 and settings.shadow < 1:
        checks.append(
            CaptionCheck(
                "contrast",
                "warning",
                "No background, outline, or shadow is set; captions may "
                "disappear over bright footage.",
            )
        )
    return checks


def _scaled_font_size(reference: int, height: int, aspect_ratio: str = "16:9") -> int:
    # Each aspect is designed against its own 1080p reference frame:
    # 1920x1080 for landscape and 1080x1920 for portrait.
    reference_height = 1920 if aspect_ratio == "9:16" else 1080
    return max(10, int(round(reference * max(240, height) / reference_height)))


def validate_cues(
    cues: list[CaptionCue],
    *,
    duration: float,
    settings: CaptionSettings,
    aspect_ratio: str,
    language: str,
    font_family: str,
) -> list[CaptionCheck]:
    checks: list[CaptionCheck] = []
    if not cues:
        return [CaptionCheck("cues_present", "failed", "No caption cues exist.")]
    checks.append(CaptionCheck("cues_present", "passed", f"{len(cues)} cue(s) generated."))
    layout = settings.layout_for(aspect_ratio)
    invalid = [
        cue.index
        for cue in cues
        if not math.isfinite(cue.start)
        or not math.isfinite(cue.end)
        or cue.start < 0
        or cue.end <= cue.start
    ]
    checks.append(
        CaptionCheck(
            "timestamps_valid",
            "failed" if invalid else "passed",
            (
                "Invalid timestamps on cue(s): " + ", ".join(str(item) for item in invalid[:8])
                if invalid
                else "All cue timestamps are finite and ordered."
            ),
        )
    )
    empty = [cue.index for cue in cues if not normalise_caption_text(cue.text)]
    checks.append(
        CaptionCheck(
            "empty_cues",
            "failed" if empty else "passed",
            (
                "Empty cue(s): " + ", ".join(str(item) for item in empty[:8])
                if empty
                else "No empty cues."
            ),
        )
    )
    overlaps: list[str] = []
    ordered = sorted(cues, key=lambda cue: cue.start)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.start < previous.end - 0.05:
            overlaps.append(f"{previous.index}→{current.index}")
    checks.append(
        CaptionCheck(
            "overlap",
            "failed" if overlaps else "passed",
            (
                "Overlapping cue(s): " + ", ".join(overlaps[:8])
                if overlaps
                else "No unintended overlaps."
            ),
        )
    )
    outside = [
        cue.index for cue in cues if cue.end > duration + 0.25 or cue.start > duration + 0.25
    ]
    checks.append(
        CaptionCheck(
            "within_media",
            "failed" if outside else "passed",
            (
                "Cue(s) extend past the narration: " + ", ".join(str(item) for item in outside[:8])
                if outside
                else f"All cues stay inside the {duration:.2f}s narration."
            ),
        )
    )
    long_lines: list[str] = []
    too_many_lines: list[str] = []
    for cue in cues:
        lines = wrap_caption_text(cue.text, layout.max_chars_per_line, layout.max_lines)
        if any(len(line) > layout.max_chars_per_line + 2 for line in lines):
            long_lines.append(str(cue.index))
        if len(lines) > layout.max_lines:
            too_many_lines.append(str(cue.index))
    checks.append(
        CaptionCheck(
            "line_length",
            "warning" if long_lines else "passed",
            (
                "Cue(s) exceed the configured line length: " + ", ".join(long_lines[:8])
                if long_lines
                else f"All lines fit within {layout.max_chars_per_line} characters."
            ),
        )
    )
    checks.append(
        CaptionCheck(
            "visible_lines",
            "failed" if too_many_lines else "passed",
            (
                "Cue(s) need more than the allowed visible lines: " + ", ".join(too_many_lines[:8])
                if too_many_lines
                else f"All cues fit within {layout.max_lines} visible line(s)."
            ),
        )
    )
    fast: list[str] = []
    short: list[str] = []
    for cue in cues:
        span = max(0.001, cue.end - cue.start)
        characters = len(normalise_caption_text(cue.text).replace(" ", ""))
        if characters / span > DEFAULT_MAX_CPS:
            fast.append(f"{cue.index} ({characters / span:.0f} cps)")
        if span < MIN_CUE_SECONDS:
            short.append(str(cue.index))
    checks.append(
        CaptionCheck(
            "reading_speed",
            "warning" if fast else "passed",
            (
                "Fast cue(s): " + ", ".join(fast[:6])
                if fast
                else "Reading speed is within the configured heuristic."
            ),
        )
    )
    checks.append(
        CaptionCheck(
            "min_duration",
            "warning" if short else "passed",
            (
                "Very short cue(s): " + ", ".join(short[:8])
                if short
                else "Cue durations are within the readable range."
            ),
        )
    )
    risky = _risky_glyphs(" ".join(cue.text for cue in cues))
    checks.append(
        CaptionCheck(
            "glyph_support",
            "warning" if risky else "passed",
            (
                "Unusual character(s) may need a font fallback: " + " ".join(sorted(risky)[:12])
                if risky
                else f"Characters are supported by {font_family}."
            ),
        )
    )
    drift = abs((cues[-1].end if cues else 0.0) - duration)
    checks.append(
        CaptionCheck(
            "synchronization",
            "warning" if drift > 1.5 else "passed",
            (
                f"Final cue ends {drift:.2f}s from the end of the narration."
                if drift > 1.5
                else f"Final cue is {drift:.2f}s from the end of the narration."
            ),
        )
    )
    return checks


def _risky_glyphs(text: str) -> set[str]:
    risky: set[str] = set()
    for character in text:
        code = ord(character)
        if character in {"\ufffd", "\u0000"}:
            risky.add(character)
            continue
        if code < 0x20:
            continue
        if (
            0x20 <= code <= 0x7E
            or 0xA0 <= code <= 0x24F
            or 0x0900 <= code <= 0x097F
            or 0x2000 <= code <= 0x206F
            or 0x20A0 <= code <= 0x20CF
            or 0x2100 <= code <= 0x214F
        ):
            continue
        if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
            risky.add(character)
        elif 0x4E00 <= code <= 0x9FFF or 0x0600 <= code <= 0x06FF:
            risky.add(character)
    return risky


# ------------------------------------------------------------------------- export


def to_srt(cues: list[CaptionCue], settings: CaptionSettings) -> str:
    layout = settings.layout_for("16:9")
    entries: list[str] = []
    for index, cue in enumerate(sorted(cues, key=lambda item: item.start), start=1):
        lines = wrap_caption_text(cue.text, layout.max_chars_per_line, layout.max_lines)
        text = "\n".join(lines) or cue.text
        if settings.speaker_labels and cue.speaker:
            text = f"{cue.speaker}: {text}"
        entries.append(
            f"{index}\n{_srt_timestamp(cue.start)} --> {_srt_timestamp(cue.end)}\n{text}"
        )
    return "\n\n".join(entries) + ("\n" if entries else "")


def to_vtt(cues: list[CaptionCue], settings: CaptionSettings, *, source: str = "") -> str:
    layout = settings.layout_for("16:9")
    lines = ["WEBVTT", ""]
    if source:
        lines.extend([f"NOTE Caption source: {source}", ""])
    for index, cue in enumerate(sorted(cues, key=lambda item: item.start), start=1):
        text_lines = wrap_caption_text(cue.text, layout.max_chars_per_line, layout.max_lines)
        text = "\n".join(text_lines) or cue.text
        if settings.speaker_labels and cue.speaker:
            text = f"<v {cue.speaker}>{text}"
        lines.append(str(index))
        lines.append(f"{_vtt_timestamp(cue.start)} --> {_vtt_timestamp(cue.end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1_000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"


def _vtt_timestamp(seconds: float) -> str:
    return _srt_timestamp(seconds).replace(",", ".")


_TIMING_LINE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})"
)


def parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = "0", parts[0], parts[1]
    else:
        raise ValueError(f"Invalid timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_srt(content: str) -> list[CaptionCue]:
    text = (content or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    cues: list[CaptionCue] = []
    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = next(
            (index for index, line in enumerate(lines) if "-->" in line),
            None,
        )
        if timing_index is None:
            continue
        match = _TIMING_LINE.search(lines[timing_index])
        if match is None:
            continue
        body = lines[timing_index + 1 :]
        if not body:
            continue
        speaker = None
        joined = " ".join(body)
        speaker_match = re.match(r"^<v\s+([^>]+)>(.*)$", joined, re.IGNORECASE)
        if speaker_match:
            speaker = speaker_match.group(1).strip()
            joined = speaker_match.group(2)
        joined = re.sub(r"<[^>]+>", "", joined)
        joined = html.unescape(joined)
        cue_text = normalise_caption_text(joined)
        if not cue_text:
            continue
        start = parse_timestamp(match.group("start"))
        end = parse_timestamp(match.group("end"))
        if end <= start:
            continue
        cues.append(
            CaptionCue(
                index=len(cues) + 1,
                start=start,
                end=end,
                text=cue_text,
                words=allocate_words(cue_text, start, end - start),
                speaker=speaker,
            )
        )
    return cues


def parse_vtt(content: str) -> list[CaptionCue]:
    text = (content or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    cues: list[CaptionCue] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            index += 1
            continue
        match = _TIMING_LINE.search(line)
        if match is None:
            index += 1
            continue
        start = parse_timestamp(match.group("start"))
        end = parse_timestamp(match.group("end"))
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(lines[index].strip())
            index += 1
        joined = " ".join(body)
        speaker = None
        speaker_match = re.match(r"^<v\s+([^>]+)>(.*)$", joined, re.IGNORECASE)
        if speaker_match:
            speaker = speaker_match.group(1).strip()
            joined = speaker_match.group(2)
        joined = re.sub(r"<[^>]+>", "", joined)
        joined = html.unescape(joined)
        cue_text = normalise_caption_text(joined)
        if cue_text and end > start:
            cues.append(
                CaptionCue(
                    index=len(cues) + 1,
                    start=start,
                    end=end,
                    text=cue_text,
                    words=allocate_words(cue_text, start, end - start),
                    speaker=speaker,
                )
            )
        index += 1
    return cues


def parse_subtitles(content: str, format: str) -> list[CaptionCue]:
    if format.lower() == "vtt":
        return parse_vtt(content)
    return parse_srt(content)


# ---------------------------------------------------------------------------- ASS


def ass_color(value: str, alpha: int = 0) -> str:
    colour = (value or "#FFFFFF").lstrip("#")
    if len(colour) != 6:
        colour = "FFFFFF"
    red, green, blue = colour[0:2], colour[2:4], colour[4:6]
    return f"&H{max(0, min(255, alpha)):02X}{blue}{green}{red}"


def _inline_color(value: str) -> str:
    colour = (value or "#FFFFFF").lstrip("#")
    if len(colour) != 6:
        colour = "FFFFFF"
    red, green, blue = colour[0:2], colour[2:4], colour[4:6]
    return f"&H{blue}{green}{red}&"


def _ass_escape(value: str) -> str:
    return value.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{whole_seconds:02}.{centiseconds:02}"


def _alignment_code(alignment: str, vertical: str) -> int:
    row = {"bottom": 0, "center": 1, "top": 2}.get(vertical, 0)
    column = {"left": 0, "center": 1, "right": 2}.get(alignment, 1)
    return row * 3 + column + 1


def _word_lines(cue: CaptionCue, layout: AspectLayout) -> list[list[CaptionWord]]:
    if cue.words:
        return _wrap_tokens(cue.words, layout.max_chars_per_line)
    tokens = [
        CaptionWord(text=word, start=cue.start, end=cue.end)
        for word in normalise_caption_text(cue.text).split()
    ]
    return _wrap_tokens(tokens, layout.max_chars_per_line)


def _render_dialogue_text(
    lines: list[list[CaptionWord]],
    *,
    highlight: int | None,
    settings: CaptionSettings,
    speaker: str | None,
) -> str:
    rendered: list[str] = []
    for line_index, line in enumerate(lines):
        words: list[str] = []
        for word_index, word in enumerate(line):
            text = _ass_escape(word.text)
            if settings.uppercase:
                text = _ass_escape(word.text.upper())
            should_highlight = (highlight is not None and word_index == highlight) or (
                settings.emphasis and word.emphasis
            )
            if should_highlight:
                text = (
                    f"{{\\c{_inline_color(settings.highlight_color)}}}"
                    f"{text}{{\\c{_inline_color(settings.text_color)}}}"
                )
            words.append(text)
        prefix = ""
        if line_index == 0 and settings.speaker_labels and speaker:
            prefix = f"{_ass_escape(speaker)}: "
        rendered.append(prefix + " ".join(words))
    return r"\N".join(rendered)


def to_ass(
    cues: list[CaptionCue],
    settings: CaptionSettings,
    *,
    aspect_ratio: str,
    width: int,
    height: int,
    font_family: str | None = None,
    language: str = "en",
) -> str:
    layout = settings.layout_for(aspect_ratio)
    font_size = _scaled_font_size(layout.font_size, height, aspect_ratio)
    margin_x = int(width * layout.safe_margin_ratio)
    margin_v = int(height * layout.safe_bottom_ratio)
    if layout.vertical_position == "top":
        margin_v = int(height * max(layout.safe_margin_ratio, 0.08))
    alignment = _alignment_code(settings.alignment, layout.vertical_position)
    background_alpha = int(round((1.0 - settings.background_opacity) * 255))
    border_style = 4 if settings.background_opacity > 0.02 else 1
    outline = (
        int(round(settings.background_padding * height / 1080))
        if border_style == 4
        else int(round(settings.outline_width * height / 1080))
    )
    shadow = int(round(settings.shadow * height / 1080))
    bold = -1 if settings.font_weight in {"semibold", "bold"} else 0
    family = font_family or settings.font_family
    fade = max(0, min(500, int(settings.fade_ms)))
    fade_tag = f"{{\\fad({fade},{fade})}}" if fade else ""

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,{family},{font_size},"
            f"{ass_color(settings.text_color)},"
            f"{ass_color(settings.text_color)},"
            f"{ass_color('#000000')},"
            f"{ass_color('#000000', background_alpha)},"
            f"{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},"
            f"{alignment},{margin_x},{margin_x},{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: list[str] = []
    ordered = sorted(cues, key=lambda cue: cue.start)
    for cue in ordered:
        lines = _word_lines(cue, layout)
        if not lines:
            continue
        if settings.word_highlight and cue.words:
            for index, word in enumerate(cue.words):
                start = max(cue.start, word.start)
                end = (
                    min(cue.end, cue.words[index + 1].start)
                    if index + 1 < len(cue.words)
                    else cue.end
                )
                if end <= start:
                    end = min(cue.end, start + 0.12)
                if end <= start:
                    continue
                # Locate the word's line and position for the highlight.
                highlight_line = None
                highlight_index = None
                for line_index, line in enumerate(lines):
                    if word in line:
                        highlight_line = line_index
                        highlight_index = line.index(word)
                        break
                text = _render_dialogue_text(
                    lines,
                    highlight=highlight_index if highlight_line is not None else None,
                    settings=settings,
                    speaker=cue.speaker,
                )
                events.append(
                    f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
                    f"{fade_tag}{text}"
                )
        else:
            text = _render_dialogue_text(
                lines,
                highlight=None,
                settings=settings,
                speaker=cue.speaker,
            )
            events.append(
                f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Default,,0,0,0,,"
                f"{fade_tag}{text}"
            )
    return "\n".join([*header, *events]) + "\n"


# --------------------------------------------------------------------------- fonts


@dataclass(frozen=True)
class FontInfo:
    family: str
    files: tuple[str, ...]
    devanagari: bool = False


FONT_LIBRARY: tuple[FontInfo, ...] = (
    FontInfo("Segoe UI", ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf")),
    FontInfo("Arial", ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf")),
    FontInfo("Georgia", ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf")),
    FontInfo("Consolas", ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf")),
    FontInfo("Nirmala UI", ("Nirmala.ttc",), devanagari=True),
    FontInfo("Noto Sans", ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf")),
    FontInfo("Noto Sans Devanagari", ("NotoSansDevanagari-Regular.ttf",), devanagari=True),
    FontInfo("DejaVu Sans", ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")),
    FontInfo("Liberation Sans", ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf")),
)

DEVANAGARI_FAMILIES = {
    "Nirmala UI",
    "Noto Sans Devanagari",
    "Mangal",
    "Kokila",
    "Aparajita",
    "Sans Serif Collection",
}


def default_fonts_dir() -> Path | None:
    candidates = []
    if sys.platform.startswith("win"):
        candidates.append(Path("C:/Windows/Fonts"))
    elif sys.platform == "darwin":
        candidates.append(Path("/Library/Fonts"))
        candidates.append(Path("/System/Library/Fonts"))
    else:
        candidates.extend([Path("/usr/share/fonts"), Path("/usr/local/share/fonts")])
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _font_file_exists(fonts_dir: Path | None, names: Iterable[str]) -> bool:
    if fonts_dir is None:
        return False
    for name in names:
        if (fonts_dir / name).is_file():
            return True
    return False


def available_fonts(fonts_dir: Path | None = None) -> list[dict[str, Any]]:
    directory = fonts_dir or default_fonts_dir()
    fonts: list[dict[str, Any]] = []
    for item in FONT_LIBRARY:
        if _font_file_exists(directory, item.files):
            fonts.append(
                {
                    "family": item.family,
                    "devanagari": item.devanagari,
                    "source": str(directory / item.files[0]) if directory else None,
                }
            )
    if not fonts:
        fonts = [
            {"family": item.family, "devanagari": item.devanagari, "source": None}
            for item in FONT_LIBRARY[:4]
        ]
    return fonts


def resolve_font_family(
    requested: str,
    *,
    language: str = "en",
    fonts_dir: Path | None = None,
) -> tuple[str, list[str]]:
    directory = fonts_dir or default_fonts_dir()
    available = {item["family"] for item in available_fonts(directory)}
    warnings: list[str] = []
    family = (requested or "Segoe UI").strip()
    needs_devanagari = language in {"hi", "hi-en"} or DEVANAGARI.search(language or "")
    if needs_devanagari and family not in DEVANAGARI_FAMILIES:
        fallback = next(
            (
                item
                for item in ("Nirmala UI", "Noto Sans Devanagari", "Mangal")
                if item in available
            ),
            None,
        )
        if fallback:
            warnings.append(
                f"{family} has no confirmed Devanagari coverage; {fallback} is used instead."
            )
            family = fallback
    if family not in available:
        fallback = next(
            (
                item
                for item in ("Segoe UI", "Arial", "Noto Sans", "DejaVu Sans", "Liberation Sans")
                if item in available
            ),
            None,
        )
        if fallback and fallback != family:
            warnings.append(f"{family} was not found; {fallback} is used instead.")
            family = fallback
        elif not fallback:
            warnings.append(
                "No installed caption font was detected. FFmpeg will use its default font."
            )
    return family, warnings


def font_supports_devanagari(family: str) -> bool:
    return family in DEVANAGARI_FAMILIES


def _settings_language_attribute(settings: CaptionSettings) -> bool:
    # Kept as a property-like helper for validation without storing language twice.
    return bool(getattr(settings, "language_is_devanagari", False))


# ``CaptionSettings`` is frozen, so expose the language flag as a read-only
# property used by validation. Pydantic/dict round-trips ignore it.
def _caption_settings_devanagari(self: CaptionSettings) -> bool:
    return font_supports_devanagari(self.font_family)


CaptionSettings.language_is_devanagari = property(  # type: ignore[attr-defined]
    _caption_settings_devanagari
)


def _unused(value: Any) -> None:
    # Keep a reference to helpers used by future providers without exporting them.
    _ = value
