from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
import trafilatura
from lingua import LanguageDetectorBuilder
from pydantic import BaseModel, Field, HttpUrl

from backend.app.schemas.article import CollectedArticle

logger = logging.getLogger(__name__)
TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "output"}


class RSSSource(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    feed_url: HttpUrl
    country: str | None = None
    category: str | None = None
    extract_full_text: bool = True
    enabled: bool = True


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",
        )
    )


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class RSSCollector:
    """RSS adapter with full-text enrichment. It has no database knowledge."""

    def __init__(self, client: httpx.AsyncClient, max_articles: int) -> None:
        self._client = client
        self._max_articles = max_articles
        self._language_detector = LanguageDetectorBuilder.from_all_languages().build()

    async def collect(self, source: RSSSource) -> tuple[list[CollectedArticle], list[str]]:
        response = await self._client.get(str(source.feed_url))
        response.raise_for_status()
        feed = await asyncio.to_thread(feedparser.parse, response.content)
        warnings: list[str] = []
        if feed.bozo:
            warnings.append(f"{source.id}: malformed RSS: {feed.bozo_exception}")

        articles: list[CollectedArticle] = []
        for entry in feed.entries[: self._max_articles]:
            try:
                article = await self._entry_to_article(source, entry)
                if article:
                    articles.append(article)
            except (httpx.HTTPError, ValueError) as exc:
                message = f"{source.id}: skipped entry: {exc}"
                logger.warning(message)
                warnings.append(message)
        return articles, warnings

    async def _entry_to_article(
        self, source: RSSSource, entry: dict[str, Any]
    ) -> CollectedArticle | None:
        link = entry.get("link")
        title = normalize_text(entry.get("title", ""))
        if not link or not title:
            return None

        summary = normalize_text(entry.get("summary", "")) or None
        raw_html: str | None = None
        extracted_text: str | None = None
        if source.extract_full_text:
            response = await self._client.get(link)
            response.raise_for_status()
            raw_html = response.text
            extracted_text = await asyncio.to_thread(
                trafilatura.extract,
                raw_html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )

        normalized_text = normalize_text(extracted_text or summary or title)
        language = self._detect_language(normalized_text)
        return CollectedArticle(
            source_id=source.id,
            source_name=source.name,
            url=link,
            canonical_url=canonicalize_url(link),
            title=title,
            summary=summary,
            raw_html=raw_html,
            normalized_text=normalized_text,
            language=language,
            category=source.category,
            country=source.country,
            published_at=self._parse_published(entry),
            metadata={"feed_entry_id": entry.get("id"), "feed_title": entry.get("title")},
        )

    def _detect_language(self, text: str) -> str | None:
        language = self._language_detector.detect_language_of(text)
        if language is None:
            return None
        return language.iso_code_639_1.name.lower()

    @staticmethod
    def _parse_published(entry: dict[str, Any]) -> datetime | None:
        for key in ("published", "updated"):
            value = entry.get(key)
            if not value:
                continue
            try:
                parsed = parsedate_to_datetime(value)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
        return None
