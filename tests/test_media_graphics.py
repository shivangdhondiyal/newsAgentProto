from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from backend.app.media.craft import platform_caption_rules
from backend.app.media.graphics import SceneVisual, render_scene_frame, wrap_text
from backend.app.media.presets import get_preset


def _visual(**overrides) -> SceneVisual:
    payload = {
        "position": 2,
        "start_seconds": 3.0,
        "duration_seconds": 6.0,
        "narration": "Officials said 42 percent of residents lost power in 2024.",
        "subtitle": "42 percent of residents lost power.",
        "headline": "Power outage affects 42 percent of the city",
        "keywords": ["earthquake", "city", "power"],
        "scene_description": "City map with outage figures.",
    }
    payload.update(overrides)
    return SceneVisual(**payload)


def test_scene_frame_renders_valid_jpeg_for_every_style_and_aspect(
    tmp_path: Path,
) -> None:
    for style in ("broadcast", "vox", "data", "social"):
        preset = get_preset(style)
        for aspect, (width, height) in {
            "16:9": (640, 360),
            "9:16": (360, 640),
        }.items():
            output = tmp_path / f"{style}-{aspect.replace(':', 'x')}.jpg"
            result = render_scene_frame(
                _visual(graphic_type="stat"),
                preset,
                width,
                height,
                output,
                aspect_ratio=aspect,
            )
            assert output.is_file()
            assert output.stat().st_size > 2_000
            with Image.open(output) as image:
                assert image.format == "JPEG"
                assert image.size == (width, height)
            assert result.graphic_type in {
                "stat",
                "map",
                "chart",
                "timeline",
                "diagram",
            }


def test_scene_frame_uses_a_real_asset_photo(tmp_path: Path) -> None:
    asset = tmp_path / "photo.jpg"
    image = Image.new("RGB", (1200, 800), (60, 90, 130))
    draw = ImageDraw.Draw(image)
    draw.rectangle((200, 150, 900, 650), fill=(210, 180, 120))
    image.save(asset, "JPEG")
    output = tmp_path / "photo-frame.jpg"
    result = render_scene_frame(
        _visual(asset_path=asset, asset_kind="photo"),
        get_preset("photojournalism"),
        640,
        360,
        output,
        aspect_ratio="16:9",
    )
    assert result.has_asset is True
    assert result.graphic_type == "photo"
    with Image.open(output) as rendered:
        assert rendered.size == (640, 360)


def test_scene_frame_falls_back_when_asset_is_missing(tmp_path: Path) -> None:
    output = tmp_path / "fallback.jpg"
    result = render_scene_frame(
        _visual(asset_path=tmp_path / "missing.jpg"),
        get_preset("broadcast"),
        640,
        360,
        output,
        aspect_ratio="16:9",
    )
    assert result.has_asset is False
    assert output.is_file()


def test_wrap_text_truncates_with_an_ellipsis() -> None:
    canvas = Image.new("RGB", (400, 200))
    draw = ImageDraw.Draw(canvas)
    from backend.app.media.graphics import _font

    font = _font("arial.ttf", 20)
    lines = wrap_text(
        draw,
        "one two three four five six seven eight nine ten eleven twelve",
        font,
        120,
        max_lines=2,
    )
    assert len(lines) == 2
    assert lines[-1].endswith("…")


def test_platform_captions_are_tighter_for_vertical_video() -> None:
    preset = get_preset("social")
    landscape = platform_caption_rules(preset.craft, "16:9")
    portrait = platform_caption_rules(preset.craft, "9:16")
    assert portrait.max_characters < landscape.max_characters
    assert portrait.safe_bottom_ratio > landscape.safe_bottom_ratio

def test_wide_shot_never_crops_outside_the_source_image() -> None:
    from backend.app.media.craft import crop_box, crop_focal

    # A portrait source with a wide-shot factor below 1 previously asked PIL
    # for a rectangle larger than the image, which padded the frame with black.
    size = (1920, 2077)
    box = crop_box(size, shot="wide-map", aspect_ratio="16:9")
    assert box[2] - box[0] <= size[0]
    assert box[3] - box[1] <= size[1]

    with Image.new("RGB", size, (120, 140, 160)) as source:
        framed = crop_focal(
            source, shot="wide-map", focal_x=0.25, focal_y=0.75, aspect_ratio="16:9"
        )
    assert framed.width <= size[0]
    assert framed.height <= size[1]
    assert framed.getpixel((framed.width - 1, framed.height // 2)) == (120, 140, 160)


def test_photo_headline_is_light_on_a_photo_backdrop(tmp_path: Path) -> None:
    asset = tmp_path / "bright.jpg"
    Image.new("RGB", (800, 450), (245, 245, 245)).save(asset, "JPEG")
    output = tmp_path / "photo.jpg"
    result = render_scene_frame(
        _visual(asset_path=asset, asset_kind="photo", headline="A short headline"),
        get_preset("photojournalism"),
        640,
        360,
        output,
        aspect_ratio="16:9",
    )
    assert result.graphic_type == "photo"
    with Image.open(output) as rendered:
        pixels = rendered.convert("RGB")
        # The headline band must stay dark enough for white type to read.
        band = pixels.crop((0, 0, 640, 120)).resize((1, 1)).getpixel((0, 0))
    assert max(band) < 200

def test_caption_is_suppressed_when_it_repeats_the_headline() -> None:
    from backend.app.media.graphics import _caption_repeats_headline

    repeated = _visual(
        headline="Lula re-elected in Brazil as Bolsonaro rejects results.",
        subtitle="Lula re-elected in Brazil as Bolsonaro rejects results.",
    )
    assert _caption_repeats_headline(repeated, get_preset("vox"))

    distinct = _visual(
        headline="Election result contested",
        subtitle="Supporters blocked a highway in Minas Gerais on Monday.",
    )
    assert not _caption_repeats_headline(distinct, get_preset("vox"))


def test_truncated_headline_ends_with_an_ellipsis() -> None:
    from backend.app.media.graphics import _headline_for

    preset = get_preset("vox")
    long_text = "word " * 60
    headline = _headline_for(_visual(headline=long_text), preset)
    assert headline.endswith("…")

def test_truncated_headline_stops_at_a_phrase_boundary() -> None:
    from backend.app.media.graphics import _truncate_headline

    text = "President Donald Trump, was re-elected Sunday in a special election"
    headline = _truncate_headline(text, 52)
    assert headline.endswith("…")
    assert not headline.rstrip("…").rstrip().endswith((" in", " a", " the", " of"))


def test_short_headline_is_returned_unchanged() -> None:
    from backend.app.media.graphics import _truncate_headline

    assert _truncate_headline("Power outage affects the city", 52) == (
        "Power outage affects the city"
    )


def test_kicker_drops_whole_keywords_instead_of_cutting_a_word() -> None:
    from backend.app.media.graphics import _fit_kicker_label, _font, _text_size

    canvas = Image.new("RGB", (1080, 1920), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = _font("arialbd.ttf", 40)
    label = _fit_kicker_label(draw, "Based · Information · Corroborated", font, 500)
    assert "CORROBORATED" not in label
    assert "COR…" not in label
    assert label == "BASED · INFORMATION"
    assert _text_size(draw, label, font)[0] <= 500


def test_headline_font_shrinks_until_the_sentence_fits() -> None:
    from backend.app.media.graphics import _fit_headline_font, _fits_lines, _font

    canvas = Image.new("RGB", (1920, 1080), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    text = "This report reflects information available from the cited sources at this time."
    full = _font("georgiab.ttf", 96)
    assert not _fits_lines(draw, text, full, 900, 2)
    fitted = _fit_headline_font(draw, text, "georgiab.ttf", 96, 900, 2)
    assert fitted.size < full.size
    assert _fits_lines(draw, text, fitted, 900, 2)
