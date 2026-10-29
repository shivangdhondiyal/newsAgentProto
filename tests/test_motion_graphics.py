from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend.app.media.genres import build_effective_profile
from backend.app.media.motion import (
    GraphicSpec,
    Keyframe,
    apply_saved_graphics,
    cubic_bezier,
    ease,
    evaluate_keyframes,
    graphic_checks,
    plan_scene_graphics,
    render_graphic_preview,
    render_overlay_sequence,
)


def test_easing_is_deterministic_and_bounded() -> None:
    for name in (
        "linear",
        "ease_in",
        "ease_out",
        "ease_in_out",
        "ease_out_cubic",
        "overshoot",
        "smooth",
    ):
        samples = [ease(name, index / 10) for index in range(11)]
        assert samples[0] == 0.0
        assert samples[-1] == 1.0
        assert samples == [ease(name, index / 10) for index in range(11)]
        assert all(-0.2 <= value <= 1.2 for value in samples)
    assert cubic_bezier(0.5, 0.42, 0.0, 0.58, 1.0) == pytest.approx(
        ease("ease_in_out", 0.5), abs=1e-3
    )


def test_keyframes_interpolate_between_real_times() -> None:
    frames = [Keyframe(0.0, 0.0), Keyframe(1.0, 10.0), Keyframe(2.0, 4.0)]
    assert evaluate_keyframes(frames, -1) == 0.0
    assert evaluate_keyframes(frames, 0.0) == 0.0
    assert evaluate_keyframes(frames, 1.0) == 10.0
    assert evaluate_keyframes(frames, 3.0) == 4.0
    linear = evaluate_keyframes(frames, 0.5, easing="linear")
    assert linear == 5.0


def test_planner_uses_only_numbers_from_the_narration() -> None:
    profile = build_effective_profile("business_economy")
    plan = plan_scene_graphics(
        scene_position=2,
        start_seconds=3.0,
        duration_seconds=7.0,
        narration="Revenue rose 12 percent while costs fell 4 percent in 2024.",
        subtitle="Revenue rose 12 percent while costs fell 4 percent in 2024.",
        keywords=["economy"],
        preset=profile.preset,
        profile=profile,
        has_asset=True,
        asset_kind="photo",
        aspect_ratio="16:9",
        captions_enabled=True,
    )
    assert plan.graphics
    chart = next(graphic for graphic in plan.graphics if graphic.kind == "chart")
    values = [item["value"] for item in chart.data]
    assert 12.0 in values
    assert 4.0 in values
    assert chart.source == "As reported"
    assert plan.checks == []


def test_planner_can_choose_stillness_for_a_quiet_scene() -> None:
    profile = build_effective_profile("breaking_news")
    plan = plan_scene_graphics(
        scene_position=3,
        start_seconds=10.0,
        duration_seconds=5.0,
        narration="Officials said the situation remains under review.",
        subtitle="Officials said the situation remains under review.",
        preset=profile.preset,
        profile=profile,
        has_asset=True,
        asset_kind="video",
        aspect_ratio="9:16",
        captions_enabled=True,
    )
    assert plan.graphics == []
    assert plan.purpose


def test_planner_never_puts_operator_licensing_text_on_screen() -> None:
    profile = build_effective_profile("science_research")
    for position in range(1, 9):
        plan = plan_scene_graphics(
            scene_position=position,
            start_seconds=float(position * 6),
            duration_seconds=6.0,
            narration="The study measured 42 percent growth in 2024 across three sites.",
            subtitle="The study measured 42 percent growth in 2024.",
            keywords=["study", "growth"],
            preset=profile.preset,
            profile=profile,
            has_asset=True,
            asset_kind="photo",
            aspect_ratio="16:9",
            captions_enabled=True,
        )
        for graphic in plan.graphics:
            assert graphic.kind != "source"
            assert "licensing panel" not in f"{graphic.text} {graphic.source}".lower()


def test_planner_never_prints_operator_notes_on_screen() -> None:
    # Source lines are drawn on the panel. "Values as stated in narration" is
    # a production note, not something a viewer should read.
    banned = (
        "as stated in narration",
        "as described in narration",
        "narration context",
        "verify against reporting",
        "licensing panel",
    )
    narration = (
        "The study measured 42 percent growth in 2024 across three sites, "
        "after the minister said \"we expect more\" in an interview."
    )
    for key in (
        "science_research",
        "business_economy",
        "data_journalism",
        "world_geopolitics",
        "politics_government",
    ):
        profile = build_effective_profile(key)
        for position in range(1, 7):
            plan = plan_scene_graphics(
                scene_position=position,
                start_seconds=float(position * 6),
                duration_seconds=6.0,
                narration=narration,
                subtitle=narration,
                keywords=["study"],
                preset=profile.preset,
                profile=profile,
                has_asset=True,
                asset_kind="photo",
            )
            for graphic in plan.graphics:
                on_screen = f"{graphic.text} {graphic.secondary_text} {graphic.source}".lower()
                assert not any(phrase in on_screen for phrase in banned), on_screen


def test_graphic_checks_flag_caption_collisions_and_missing_data() -> None:
    spec = GraphicSpec(
        id="g-1",
        kind="chart",
        scene_position=1,
        start_seconds=0.2,
        duration_seconds=3.0,
        y=0.82,
        height=0.16,
        data=[],
    )
    checks = graphic_checks(
        [spec],
        aspect_ratio="16:9",
        captions_enabled=True,
        scene_start=0.0,
        scene_end=5.0,
    )
    names = {check["name"]: check["status"] for check in checks}
    assert names["g-1_caption_overlap"] == "warning"
    assert names["g-1_data"] == "failed"
    assert names["g-1_source"] == "warning"


def test_overlay_sequence_is_deterministic_and_transparent(tmp_path: Path) -> None:
    profile = build_effective_profile("technology_ai")
    spec = GraphicSpec(
        id="g-test",
        kind="lower_third",
        scene_position=1,
        start_seconds=1.0,
        duration_seconds=1.0,
        text="Test graphic",
        secondary_text="Technology & AI",
    )
    first = render_overlay_sequence(
        spec,
        profile.preset,
        width=320,
        height=180,
        fps=10,
        output_dir=tmp_path / "first",
    )
    second = render_overlay_sequence(
        spec,
        profile.preset,
        width=320,
        height=180,
        fps=10,
        output_dir=tmp_path / "second",
    )
    assert first.frame_count == 10
    assert (first.directory / "frame-00000.png").is_file()
    assert first.pattern.name == "frame-%05d.png"
    first_frame = Image.open(first.directory / "frame-00000.png")
    second_frame = Image.open(second.directory / "frame-00000.png")
    assert first_frame.mode == "RGBA"
    assert list(first_frame.getdata()) == list(second_frame.getdata())


def test_preview_renders_a_real_png(tmp_path: Path) -> None:
    profile = build_effective_profile("data_journalism")
    spec = GraphicSpec(
        id="g-preview",
        kind="stat",
        scene_position=1,
        start_seconds=0.0,
        duration_seconds=3.0,
        value="12%",
        text="Reported increase",
    )
    output = render_graphic_preview(
        spec,
        profile.preset,
        tmp_path / "preview.png",
        width=480,
        height=270,
    )
    assert output.is_file()
    with Image.open(output) as image:
        assert image.size == (480, 270)


def test_overlays_do_not_repeat_the_frames_own_panel() -> None:
    from types import SimpleNamespace

    from backend.app.media.motion import plan_graphics_for_timeline

    profile = build_effective_profile("world_geopolitics")
    narrations = [
        "Fighting in the Russian-occupied city of Oleshky has stopped after a truce.",
        "The White House said the truce was secured in the city of Oleshky.",
        "The truce is meant to allow aid deliveries in the city on Friday.",
        "Residents told the BBC they were weary of the ceasefire near Crimea.",
        "The BBC correspondent has more details from the region.",
    ]
    scenes = [
        SimpleNamespace(
            position=index + 1,
            narration=text,
            subtitle=text,
            keywords=["oleshky", "ukraine", "truce"],
        )
        for index, text in enumerate(narrations)
    ]
    timings = [
        SimpleNamespace(start_seconds=index * 6.0, duration_seconds=6.0)
        for index in range(len(scenes))
    ]
    frame_types = {1: "map", 2: "typography", 3: "map", 4: "photo", 5: "typography"}
    plans = plan_graphics_for_timeline(
        scenes=scenes,
        timings=timings,
        preset=profile.preset,
        profile=profile,
        asset_lookup={position: (position == 4, "photo") for position in frame_types},
        aspect_ratio="16:9",
        captions_enabled=True,
        frame_types=frame_types,
    )
    by_position = {plan.scene_position: plan for plan in plans}
    panel_kinds = {"map", "chart", "timeline", "diagram", "stat", "quote", "comparison", "split"}
    # A frame that already carries a map panel must not get a second one.
    assert not [g for g in by_position[1].graphics if g.kind in panel_kinds]
    # A licensed photo keeps its own evidence instead of being covered by a panel.
    assert not [g for g in by_position[4].graphics if g.kind in panel_kinds]
    # Between two map frames the locator overlay is dropped rather than shrunk
    # into an unreadable strip. The frame may still get a real identification.
    assert all(graphic.kind not in panel_kinds for graphic in by_position[2].graphics)
    assert all(check.get("status") != "failed" for plan in plans for check in plan.checks)


def test_comparison_requires_two_real_figures() -> None:
    # "after" alone used to produce a Before/After panel with no values, which
    # is invented data. Without two figures the planner must stay quiet.
    profile = build_effective_profile("business_economy")
    plan = plan_scene_graphics(
        scene_position=2,
        start_seconds=0.0,
        duration_seconds=6.0,
        narration="The company said the deal came after a long negotiation.",
        subtitle="The company said the deal came after a long negotiation.",
        preset=profile.preset,
        profile=profile,
        has_asset=False,
    )
    assert all(graphic.kind != "comparison" for graphic in plan.graphics)


def test_identification_names_only_real_people_or_institutions() -> None:
    profile = build_effective_profile("world_geopolitics")
    named = plan_scene_graphics(
        scene_position=5,
        start_seconds=24.0,
        duration_seconds=6.0,
        narration="BBC's Ukraine correspondent, Dan Johnson, has more details.",
        subtitle="BBC's Ukraine correspondent, Dan Johnson, has more details.",
        preset=profile.preset,
        profile=profile,
        has_asset=False,
        frame_type="typography",
    )
    lower = [graphic for graphic in named.graphics if graphic.kind == "lower_third"]
    assert [graphic.text for graphic in lower] == ["Dan Johnson"]
    # The second line is the role the narration gives, never the genre name.
    assert lower[0].secondary_text == "Ukraine correspondent"
    assert "geopolitics" not in lower[0].secondary_text.lower()

    # A capitalised adjective phrase is not a name and must not become a chyron.
    adjective = plan_scene_graphics(
        scene_position=2,
        start_seconds=0.0,
        duration_seconds=6.0,
        narration="Fighting in the Russian-occupied Ukrainian city has stopped.",
        subtitle="Fighting in the Russian-occupied Ukrainian city has stopped.",
        preset=profile.preset,
        profile=profile,
        has_asset=False,
        frame_type="typography",
    )
    assert all(graphic.kind != "lower_third" for graphic in adjective.graphics)

    # Sensitive profiles stay restrained even when a name is present.
    disaster = build_effective_profile("disaster_humanitarian")
    quiet = plan_scene_graphics(
        scene_position=2,
        start_seconds=0.0,
        duration_seconds=6.0,
        narration="Maria Santos said the rescue teams arrived after dawn.",
        subtitle="Maria Santos said the rescue teams arrived after dawn.",
        preset=disaster.preset,
        profile=disaster,
        has_asset=False,
        frame_type="typography",
    )
    assert all(graphic.kind != "lower_third" for graphic in quiet.graphics)


def test_identification_does_not_invent_a_role() -> None:
    profile = build_effective_profile("world_geopolitics")
    text = (
        "Fighting has stopped, which the White House said was secured "
        "by the US First Lady."
    )
    plan = plan_scene_graphics(
        scene_position=2,
        start_seconds=0.0,
        duration_seconds=6.0,
        narration=text,
        subtitle=text,
        preset=profile.preset,
        profile=profile,
        has_asset=False,
        frame_type="typography",
    )
    lower = [graphic for graphic in plan.graphics if graphic.kind == "lower_third"]
    assert [graphic.text for graphic in lower] == ["White House"]
    # No role is stated for the institution, so the chyron shows no second line.
    assert lower[0].secondary_text == ""


def test_saved_edits_survive_replanning() -> None:
    profile = build_effective_profile("technology_ai")
    original = plan_scene_graphics(
        scene_position=2,
        start_seconds=0.0,
        duration_seconds=6.0,
        narration="The system processes requests in three steps.",
        subtitle="The system processes requests in three steps.",
        preset=profile.preset,
        profile=profile,
        has_asset=True,
        asset_kind="photo",
    )
    assert original.graphics
    edited = original.graphics[0].to_dict()
    edited["text"] = "Operator-edited label"
    edited["enabled"] = False
    edited["edited"] = True
    merged = apply_saved_graphics(
        original,
        {"graphics": [edited]},
    )
    restored = next(graphic for graphic in merged.graphics if graphic.id == original.graphics[0].id)
    assert restored.text == "Operator-edited label"
    assert restored.enabled is False
