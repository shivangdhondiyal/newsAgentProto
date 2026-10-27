from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from backend.app.media.assets import (
    AssetBrief,
    AssetCandidate,
    AssetValidationError,
    license_allowed,
    rank_candidate,
    relevance_score,
    sniff_image,
    subject_match,
    validate_image_bytes,
)


def _png(width: int, height: int, color: tuple[int, int, int] = (30, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_license_filter_accepts_reusable_and_rejects_restricted() -> None:
    assert license_allowed("CC BY-SA 4.0")
    assert license_allowed("Public domain")
    assert license_allowed("NASA Media Usage Guidelines")
    assert not license_allowed("All rights reserved")
    assert not license_allowed("Fair use")
    assert not license_allowed(None)


def test_signature_check_rejects_html_disguised_as_an_image() -> None:
    assert sniff_image(b"<html><body>blocked</body></html>") is None
    assert sniff_image(_png(8, 8)) == ("png", "image/png")


def test_validate_image_rejects_html_and_small_dimensions() -> None:
    with pytest.raises(AssetValidationError, match="not a supported image format"):
        validate_image_bytes(
            b"<html>not an image</html>",
            min_width=100,
            min_height=100,
            max_bytes=1_000_000,
        )
    with pytest.raises(AssetValidationError, match="minimum is"):
        validate_image_bytes(
            _png(40, 40),
            min_width=640,
            min_height=360,
            max_bytes=1_000_000,
        )


def test_validate_image_returns_real_dimensions_and_focal_point() -> None:
    validated = validate_image_bytes(
        _png(1280, 720),
        min_width=640,
        min_height=360,
        max_bytes=2_000_000,
    )
    assert (validated.width, validated.height) == (1280, 720)
    assert validated.mime_type == "image/png"
    assert 0.0 <= validated.focal_x <= 1.0
    assert 0.0 <= validated.focal_y <= 1.0


def test_rank_prefers_keyword_match_and_larger_images() -> None:
    brief = AssetBrief(
        scene_id="scene-1",
        query="earthquake city damage",
        keywords=["earthquake", "city"],
        description="Rescue crews in a damaged city",
    )
    matching = AssetCandidate(
        provider="wikimedia",
        provider_asset_id="a",
        title="Earthquake damage in the city",
        description="Rescue crews search a collapsed building",
        source_url="https://example.org/a",
        download_url="https://example.org/a.jpg",
        creator="Photographer",
        license_name="CC BY-SA 4.0",
        license_url=None,
        attribution=None,
        width=2400,
        height=1600,
    )
    unrelated = AssetCandidate(
        provider="wikimedia",
        provider_asset_id="b",
        title="Mountain lake",
        description="A calm lake",
        source_url="https://example.org/b",
        download_url="https://example.org/b.jpg",
        creator="Photographer",
        license_name="CC BY-SA 4.0",
        license_url=None,
        attribution=None,
        width=640,
        height=360,
    )
    assert rank_candidate(matching, brief) > rank_candidate(unrelated, brief)


def test_normalize_image_writes_a_bounded_jpeg(tmp_path: Path) -> None:
    from backend.app.media.assets import normalize_image

    validated = validate_image_bytes(
        _png(1600, 900),
        min_width=640,
        min_height=360,
        max_bytes=2_000_000,
    )
    output = normalize_image(validated, tmp_path / "asset.jpg")
    assert output.is_file()
    with Image.open(output) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 1920


def _candidate(title: str, description: str = "") -> AssetCandidate:
    return AssetCandidate(
        provider="wikimedia",
        provider_asset_id=title,
        title=title,
        description=description,
        source_url="https://commons.example/file",
        download_url="https://commons.example/file.jpg",
        creator="Someone",
        license_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        attribution="Someone, CC BY-SA 4.0",
        width=1920,
        height=1080,
    )


def test_subjectless_overlap_does_not_count_as_relevance() -> None:
    brief = AssetBrief(
        scene_id="scene",
        query="based information corroborated",
        keywords=["based", "information", "corroborated"],
    )
    botanical = _candidate(
        "Despite previous and Field surveys - Sociedade Botanica do Brasil - 001.jpg",
        "Figure reporting survey information and sources.",
    )

    # Every word that matches is filler, so the candidate must not be usable.
    assert relevance_score(botanical, brief) < 0.35


def test_subject_match_rejects_licensed_but_off_subject_images() -> None:
    brief = AssetBrief(
        scene_id="scene",
        query="lula brazil bolsonaro",
        keywords=["lula", "brazil", "bolsonaro"],
        subjects=("lula", "brazil", "bolsonaro"),
    )
    village = _candidate("Dulmen, Hausdulmen, Kettbach -- 2015 -- 8499-503.jpg")
    lula = _candidate("Lula e Bolsonaro durante debate presidencial.jpg")

    assert not subject_match(village, brief)
    assert subject_match(lula, brief)
    assert relevance_score(lula, brief) >= 0.35


def test_subject_match_is_skipped_without_named_entities() -> None:
    brief = AssetBrief(
        scene_id="scene",
        query="protests france unions",
        keywords=["protests", "france", "unions"],
    )
    assert subject_match(_candidate("Anything at all"), brief)
