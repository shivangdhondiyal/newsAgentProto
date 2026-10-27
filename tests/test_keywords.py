from __future__ import annotations

from backend.app.media.keywords import proper_nouns, search_keywords


def test_search_keywords_prefers_named_entities() -> None:
    keywords = search_keywords(
        "Far-left leader Jean-Luc Melenchon said almost 1,000 people had "
        "been arrested after clashes with police in Paris."
    )

    assert keywords[:3] == ["jean-luc", "melenchon", "paris"]
    assert "said" not in keywords
    assert "almost" not in keywords
    assert len(keywords) == 5


def test_search_keywords_keeps_concrete_nouns() -> None:
    assert search_keywords(
        "Fresh protests across France as unions call for a general strike."
    ) == ["france", "protests", "unions", "general", "strike"]


def test_search_keywords_returns_nothing_for_filler() -> None:
    assert search_keywords("Here is what is known.") == []
    assert search_keywords("") == []


def test_search_keywords_respects_limit() -> None:
    keywords = search_keywords(
        "The summit in Paris brought Berlin, Madrid and Rome together", limit=2
    )

    assert keywords == ["paris", "berlin"]


def test_proper_nouns_prefers_named_entities() -> None:
    assert proper_nouns(
        "Here is what is known so far about Lula re-elected in Brazil as "
        "Bolsonaro rejects results."
    ) == ["lula", "brazil", "bolsonaro"]


def test_proper_nouns_ignores_sentence_openers_and_filler() -> None:
    assert proper_nouns("This report is based on information from 3 sources.") == []
    assert proper_nouns("Fresh protests across France as unions strike.") == ["france"]
