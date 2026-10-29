from __future__ import annotations

import re

# Lines that describe the production rather than the story. They belong in the
# credibility panel, never in an on-screen headline.
_META_SENTENCE = re.compile(
    r"^(?:"
    r"here is what is known so far(?: about)?\b|"
    r"this report (?:is based on|reflects)\b|"
    r"this (?:story|summary|report) (?:is based on|reflects)\b|"
    r"the following (?:report|summary) (?:is|reflects)\b|"
    r"information (?:was )?corroborated by\b|"
    r"based on information corroborated by\b|"
    r"for (?:more )?context\b|"
    r"editor'?s note\b"
    r")",
    re.IGNORECASE,
)

_META_PREFIX = re.compile(r"^here is what is known so far about\s+", re.IGNORECASE)

# Scraped pages sometimes carry player/consent furniture into the extracted
# text. It is not part of the story and must never reach a headline, the
# narration, or genre detection (a "video player" is not a sports signal).
_SITE_BOILERPLATE = re.compile(
    r"(?:browser extensions?|video player|disable it on this site|"
    r"to watch this content|enable javascript|cookie settings|sign up for)",
    re.IGNORECASE,
)


def strip_meta_sentences(text: str) -> str:
    """Remove production/credibility boilerplate from display copy.

    The script writer sometimes prepends context sentences ("Here is what is
    known so far about ...", "This report is based on ..."). They are useful
    for the credibility panel but unreadable as a headline, so they are removed
    here without touching the narration that is actually spoken.
    """

    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    cleaned = _META_PREFIX.sub("", cleaned)
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    kept = [sentence for sentence in sentences if sentence and not _META_SENTENCE.match(sentence)]
    kept = [sentence for sentence in kept if not _SITE_BOILERPLATE.search(sentence)]
    return " ".join(kept).strip()


def strip_leading_headline(text: str, headline: str) -> str:
    """Drop a story headline that was concatenated onto the first sentence.

    The script writer sometimes joins the headline and the lead sentence with
    only a space ("What we know about X Fighting in the city has stopped").
    Reading that aloud or on screen repeats the title, so the prefix is removed
    when the remainder is still a usable sentence.
    """

    cleaned = " ".join((text or "").split())
    title = " ".join((headline or "").split())
    if not cleaned or not title:
        return cleaned
    if not cleaned.lower().startswith(title.lower()):
        return cleaned
    remainder = cleaned[len(title) :].lstrip(" .,:;-–—")
    if len(remainder) < 24:
        return cleaned
    return remainder[0].upper() + remainder[1:]


_TRAILING_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "by",
        "from",
        "as",
        "that",
        "which",
        "who",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
    }
)


_BROADCAST_FILLER = re.compile(
    r"[,;]?\s*\b(?:has|have|with)\s+(?:more|the latest)\b.*$",
    re.IGNORECASE,
)


def headline_for_text(text: str, limit: int) -> str:
    """Derive one short, readable headline phrase from a scene's narration.

    A headline is read at a glance, so it stops on a word boundary and never
    ends on a dangling preposition or conjunction. The caption still carries
    the complete sentence.
    """

    cleaned = strip_meta_sentences(text)
    if not cleaned:
        return ""
    # "Dan Johnson has more details" is a hand-off to the next scene, not a
    # headline. Drop the broadcast filler and keep the identification.
    cleaned = _BROADCAST_FILLER.sub("", cleaned).strip(" ,;:-")
    if not cleaned:
        return ""
    limit = max(24, int(limit))
    headline = cleaned
    if len(headline) > limit:
        cut = headline[:limit].rstrip()
        headline = cut.rsplit(" ", 1)[0] if " " in cut else cut
    words = headline.split()
    while len(words) > 3 and words[-1].lower().strip(",;:.") in _TRAILING_FUNCTION_WORDS:
        words.pop()
    return " ".join(words).rstrip(" ,;:-")
