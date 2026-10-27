"""Searchable keyword extraction shared by scene planning and asset retrieval.

Image search engines respond badly to reporting verbs, numbers, and function
words. This module reduces a sentence to a short list of concrete subjects,
preferring named entities that appear mid-sentence (where capitalisation is a
reliable signal).
"""

from __future__ import annotations

import re

STOP_WORDS = {
    "about",
    "across",
    "after",
    "against",
    "ahead",
    "all",
    "almost",
    "also",
    "although",
    "amid",
    "among",
    "an",
    "and",
    "any",
    "are",
    "around",
    "as",
    "at",
    "back",
    "be",
    "because",
    "been",
    "before",
    "behind",
    "being",
    "between",
    "both",
    "but",
    "by",
    "call",
    "called",
    "calls",
    "can",
    "could",
    "daily",
    "did",
    "do",
    "does",
    "during",
    "each",
    "earlier",
    "early",
    "even",
    "ever",
    "every",
    "few",
    "first",
    "for",
    "former",
    "fresh",
    "from",
    "get",
    "gets",
    "give",
    "given",
    "goes",
    "going",
    "good",
    "great",
    "had",
    "half",
    "has",
    "have",
    "he",
    "her",
    "here",
    "high",
    "higher",
    "his",
    "how",
    "however",
    "if",
    "in",
    "including",
    "into",
    "is",
    "it",
    "its",
    "just",
    "far",
    "known",
    "large",
    "last",
    "late",
    "later",
    "least",
    "less",
    "like",
    "little",
    "long",
    "low",
    "made",
    "make",
    "makes",
    "many",
    "may",
    "might",
    "more",
    "most",
    "much",
    "must",
    "my",
    "nearly",
    "need",
    "needed",
    "new",
    "next",
    "no",
    "not",
    "now",
    "number",
    "of",
    "often",
    "on",
    "once",
    "one",
    "only",
    "or",
    "other",
    "our",
    "out",
    "over",
    "own",
    "part",
    "people",
    "plus",
    "put",
    "rather",
    "really",
    "recent",
    "report",
    "reported",
    "right",
    "said",
    "same",
    "say",
    "says",
    "second",
    "see",
    "seen",
    "several",
    "she",
    "should",
    "since",
    "so",
    "some",
    "still",
    "such",
    "take",
    "taken",
    "takes",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "third",
    "this",
    "those",
    "though",
    "thousand",
    "thousands",
    "through",
    "time",
    "to",
    "too",
    "two",
    "under",
    "until",
    "up",
    "use",
    "used",
    "using",
    "various",
    "very",
    "was",
    "we",
    "well",
    "were",
    "what",
    "when",
    "where",
    "whether",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "within",
    "without",
    "would",
    "year",
    "years",
    "you",
    "your",
}

WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z'-]+")
SENTENCE_END = ".!?:;\"'()"

# Capitalised words that open a sentence without being a name.
SENTENCE_STARTERS = {
    "a",
    "after",
    "also",
    "an",
    "and",
    "another",
    "as",
    "at",
    "before",
    "both",
    "but",
    "by",
    "despite",
    "during",
    "each",
    "earlier",
    "even",
    "every",
    "for",
    "fresh",
    "from",
    "here",
    "his",
    "how",
    "however",
    "in",
    "it",
    "its",
    "just",
    "last",
    "later",
    "meanwhile",
    "more",
    "most",
    "new",
    "next",
    "no",
    "not",
    "now",
    "of",
    "on",
    "once",
    "one",
    "only",
    "or",
    "other",
    "our",
    "over",
    "police",
    "president",
    "prime",
    "since",
    "some",
    "still",
    "such",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "three",
    "through",
    "to",
    "two",
    "under",
    "until",
    "video",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "with",
    "without",
    "yet",
}


def proper_nouns(text: str, limit: int = 4) -> list[str]:
    """Return the named entities in ``text`` (people, places, organisations).

    Only these terms are strong enough evidence that a photo library result is
    about the same thing; matching on words like "report" or "sources" is what
    produced a botanical survey figure over an election story.
    """

    source = text or ""
    found: list[str] = []
    seen: set[str] = set()
    previous_end = 0
    for match in WORD_PATTERN.finditer(source):
        word = match.group(0)
        lower = word.lower()
        gap = source[previous_end : match.start()]
        previous_end = match.end()
        if lower in STOP_WORDS or lower in seen or not word[0].isupper():
            continue
        at_sentence_start = (
            not source[: match.start()].strip()
            or "\n" in gap
            or any(char in SENTENCE_END for char in gap)
        )
        if at_sentence_start and lower in SENTENCE_STARTERS:
            continue
        seen.add(lower)
        found.append(lower)
        if len(found) == limit:
            break
    return found


def search_keywords(text: str, limit: int = 5) -> list[str]:
    """Return at most ``limit`` concrete search terms from ``text``.

    Mid-sentence capitalised words are treated as named entities and surface
    first, because "Macron" or "Paris" identifies a subject far better than the
    most common lowercase noun in the sentence.
    """

    proper: list[str] = []
    general: list[str] = []
    seen: set[str] = set()
    previous_end = 0
    source = text or ""
    for match in WORD_PATTERN.finditer(source):
        word = match.group(0)
        lower = word.lower()
        gap = source[previous_end : match.start()]
        previous_end = match.end()
        if lower in STOP_WORDS or lower in seen:
            continue
        first_word = not source[: match.start()].strip()
        at_sentence_start = (
            first_word or "\n" in gap or any(char in SENTENCE_END for char in gap)
        )
        if not at_sentence_start and word[0].isupper():
            proper.append(lower)
        else:
            general.append(lower)
        seen.add(lower)
    keywords: list[str] = []
    for candidate in [*proper[:3], *general, *proper[3:]]:
        if candidate not in keywords:
            keywords.append(candidate)
        if len(keywords) == limit:
            break
    return keywords
