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

# Splitting on every period breaks "brokered by U.S. first lady" into two
# fragments and reads as two unrelated sentences. Common abbreviations and
# initials keep their sentence together.
_SENTENCE_SPLIT = re.compile(
    r"(?<!\bU\.S)(?<!\bU\.K)(?<!\bU\.N)(?<!\bMr)(?<!\bMrs)(?<!\bMs)"
    r"(?<!\bDr)(?<!\bProf)(?<!\bSt)(?<!\bJr)(?<!\bSr)"
    r"(?<!\ba\.m)(?<!\bp\.m)(?<![A-Z]\.)(?<=[.!?])\s+"
)


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences without breaking on common abbreviations."""

    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT.split(cleaned) if part.strip()]

# Wire copy often opens with a dateline ("MYKOLAIV, Ukraine — "). It is a
# production artefact, not something a presenter says or a viewer needs burned
# into the caption, so it is removed from spoken and displayed copy.
_DATELINE = re.compile(
    r"^(?:[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3}),\s*"
    r"(?:[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})\s*[—–-]{1,2}\s+"
)

# Scraped pages sometimes carry player/consent furniture into the extracted
# text. It is not part of the story and must never reach a headline, the
# narration, or genre detection (a "video player" is not a sports signal).
_SITE_BOILERPLATE = re.compile(
    r"(?:browser extensions?|video player|disable it on this site|"
    r"to watch this content|enable javascript|cookie settings|sign up for)",
    re.IGNORECASE,
)

# Scraped pages also carry publication labels and podcast/credit blocks:
# "Published <lead>", "Episode details Available for over a year <lead>",
# "Presenter: … Producer: … (Image: …) Programme Website". None of that is
# story content, so it is removed before sentences become key facts.
_LEAD_LABEL = re.compile(
    r"^(?:published|updated|episode details|available for over a year|"
    r"image source|image caption)\b[\s:–—-]*",
    re.IGNORECASE,
)
_IMAGE_TAIL = re.compile(r"\((?:image|photo|credit)\b.*$", re.IGNORECASE)
_CREDIT_TAIL = re.compile(
    r"\b(?:presenter|producer|reporter|editor|credits?)\s*:.*$",
    re.IGNORECASE,
)
_PROGRAMME_TAIL = re.compile(r"\bprogramme website\b.*$", re.IGNORECASE)


def strip_article_furniture(text: str) -> str:
    """Remove page furniture that survives article extraction.

    Labels and credit blocks are stripped repeatedly because they can stack
    ("Episode details Available for over a year …"). The remainder is returned
    with collapsed whitespace so sentence splitting sees clean prose.
    """

    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    cleaned = _IMAGE_TAIL.sub("", cleaned)
    cleaned = _CREDIT_TAIL.sub("", cleaned)
    cleaned = _PROGRAMME_TAIL.sub("", cleaned)
    for _ in range(4):
        stripped = _LEAD_LABEL.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned.strip(" ,;:-–—")


def strip_meta_sentences(text: str) -> str:
    """Remove production/credibility boilerplate from display copy.

    The script writer sometimes prepends context sentences ("Here is what is
    known so far about ...", "This report is based on ..."). They are useful
    for the credibility panel but unreadable as a headline, so they are removed
    here without touching the narration that is actually spoken.
    """

    cleaned = strip_article_furniture(text)
    if not cleaned:
        return ""
    cleaned = _META_PREFIX.sub("", cleaned)
    sentences = split_sentences(cleaned)
    kept = [sentence for sentence in sentences if sentence and not _META_SENTENCE.match(sentence)]
    kept = [sentence for sentence in kept if not _SITE_BOILERPLATE.search(sentence)]
    return " ".join(kept).strip()


def strip_dateline(text: str) -> str:
    """Drop a leading wire-service dateline such as ``MYKOLAIV, Ukraine —``."""

    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    match = _DATELINE.match(cleaned)
    if not match:
        return cleaned
    remainder = cleaned[match.end() :].lstrip()
    # Only remove it when a real sentence follows; otherwise the "dateline" may
    # be the only content and dropping it would leave an empty scene.
    if len(remainder) < 24:
        return cleaned
    return remainder[0].upper() + remainder[1:]


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
