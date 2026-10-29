from __future__ import annotations

import pytest

from backend.app.media.captions import CAPTION_PRESETS
from backend.app.media.genres import (
    GENRES,
    build_effective_profile,
    genre_summary,
    list_genres,
    recommend_genre,
    resolve_genre,
)


def test_every_genre_resolves_to_a_supported_preset() -> None:
    assert len(GENRES) == 16
    for key, genre in GENRES.items():
        profile = build_effective_profile(key)
        assert profile.genre.key == key
        assert profile.preset.key in {
            "broadcast",
            "breaking",
            "data",
            "diagram",
            "documentary",
            "map_geopolitical",
            "photojournalism",
            "magazine",
            "vox",
            "interview",
        }
        assert genre.caption_preset in CAPTION_PRESETS
        assert profile.caption_preset in CAPTION_PRESETS
        summary = genre_summary(profile)
        assert summary["genre_key"] == key
        assert summary["rationale"]


def test_list_genres_is_public_and_complete() -> None:
    genres = list_genres()
    assert len(genres) == 16
    sample = next(item for item in genres if item["key"] == "business_economy")
    assert sample["name"] == "Business & Economy"
    assert sample["default_style"] == "data"
    assert sample["visuals"]["graphic_templates"]
    assert sample["audio"]["target_lufs"] <= -12


def test_auto_recommendation_uses_story_text_and_explains_itself() -> None:
    recommendation = recommend_genre(
        "Markets rallied after inflation data showed company earnings rising."
    )
    assert recommendation.genre.key == "business_economy"
    assert "business" in recommendation.rationale.lower()

    fallback = recommend_genre("A quiet afternoon in the park.")
    assert fallback.genre.key == "general_news"
    assert "safest default" in fallback.rationale


def test_auto_recommendation_matches_whole_words_only() -> None:
    # "said" and "against" contain the letters "ai" but are not AI stories.
    recommendation = recommend_genre(
        "Officials said a ceasefire was holding against the border, and aid remained."
    )
    assert recommendation.genre.key == "world_geopolitics"
    assert "ai" not in recommendation.rationale.lower().split("mentions")[1]


def test_generic_question_stem_does_not_outrank_a_subject_match() -> None:
    # "Here is what is known ..." used to recommend the explainer profile for
    # a war story just because it contained the phrase "what is".
    text = (
        "Here is what is known so far about What we know about Ukraine-Russia truce. "
        "Fighting has stopped after a truce. Residents were weary of the ceasefire."
    )
    recommendation = recommend_genre(text)
    assert recommendation.genre.key == "world_geopolitics"


def test_scraped_player_boilerplate_does_not_choose_a_genre() -> None:
    # A video page's player furniture used to match the sports hint "player"
    # and label a fatal train crash as Sports News.
    text = (
        "Spain high-speed train crash kills 68, injures over 100. "
        "This report is based on information corroborated by 3 independent sources. "
        "One of your browser extensions seems to be blocking the video player from loading. "
        "To watch this content, you may need to disable it on this site."
    )
    recommendation = recommend_genre(text)
    assert recommendation.genre.key == "disaster_humanitarian"
    assert "train crash" in recommendation.rationale


def test_generic_qualifiers_do_not_decide_a_genre_alone() -> None:
    # "alleged" and "series" are ordinary words in any story; on their own they
    # must not turn a casualty report into a crime or entertainment story.
    assert (
        recommend_genre(
            "More than 60 Christians killed in Ethiopia massacre, church official says. "
            "People were killed in a series of attacks in Oromia."
        ).genre.key
        == "general_news"
    )
    assert (
        recommend_genre(
            "US military strike on alleged drug vessel kills five people in the Pacific."
        ).genre.key
        == "general_news"
    )


def test_explicit_genre_beats_auto_and_unknown_keys_fail() -> None:
    recommendation = resolve_genre("sports_news", story_text="inflation and markets")
    assert recommendation.genre.key == "sports_news"
    assert recommendation.automatic is False

    with pytest.raises(ValueError, match="Unknown news genre"):
        build_effective_profile("not_a_genre")


def test_presentation_style_is_an_override_not_a_genre_change() -> None:
    default = build_effective_profile("business_economy")
    assert default.preset.key == "data"
    assert default.style_overridden is False

    overridden = build_effective_profile("business_economy", style_key="vox")
    assert overridden.genre.key == "business_economy"
    assert overridden.preset.key == "vox"
    assert overridden.style_overridden is True
    assert overridden.preset.craft.shot_mix == overridden.genre.shot_mix


def test_sensitive_genres_never_get_decorative_music_or_sfx() -> None:
    profile = build_effective_profile(
        "disaster_humanitarian",
        overrides={"music_enabled": True, "sfx_enabled": True},
    )
    assert profile.genre.sensitivity == "high"
    assert profile.music_enabled is False
    assert profile.sfx_enabled is False


def test_overrides_are_clamped_and_applied_last() -> None:
    profile = build_effective_profile(
        "general_news",
        overrides={
            "motion_intensity": "expressive",
            "music_level": 4.0,
            "narration_level": -2,
            "target_lufs": -60,
        },
    )
    assert 0.0 <= profile.motion_intensity <= 1.0
    assert profile.music_level == 1.0
    assert profile.narration_level == 0.0
    assert profile.target_lufs == -24.0
    assert profile.overrides["music_level"] == 4.0


def test_genre_defaults_change_actual_render_and_audio_settings() -> None:
    breaking = build_effective_profile("breaking_news")
    documentary = build_effective_profile("documentary_deep_dive")
    assert breaking.preset.transition != documentary.preset.transition
    assert breaking.music_enabled is False
    assert documentary.music_enabled is True
    assert breaking.motion_intensity < documentary.motion_intensity
