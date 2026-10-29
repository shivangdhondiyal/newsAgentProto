from __future__ import annotations

import hashlib
import html
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from PIL import Image, ImageFilter, UnidentifiedImageError

from backend.app.core.config import Settings

ALLOWED_LICENSE = re.compile(
    r"(public domain|cc0|cc by|cc-by|attribution|no known copyright|"
    r"nasa media usage|pexels license|pixabay content license)",
    re.IGNORECASE,
)
BLOCKED_LICENSE = re.compile(
    r"(non-free|fair use|all rights reserved|gfdl|copyrighted|editorial use only)",
    re.IGNORECASE,
)
HTML_TAG = re.compile(r"<[^>]+>")
PROVIDER_RELIABILITY = {
    "wikimedia": 0.88,
    "nasa": 0.96,
    "pexels": 0.82,
    "pixabay": 0.78,
}
IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)
MAX_IMAGE_PIXELS = 80_000_000


class AssetValidationError(ValueError):
    """A downloaded file failed signature, decode, size, or dimension checks."""


@dataclass(frozen=True)
class AssetBrief:
    scene_id: str
    query: str
    keywords: list[str]
    subjects: tuple[str, ...] = ()
    description: str = ""
    preferred_types: tuple[str, ...] = ("photograph",)
    prefer_aspect: str = "16:9"
    min_width: int = 640
    min_height: int = 360


@dataclass
class AssetCandidate:
    provider: str
    provider_asset_id: str
    title: str
    description: str
    source_url: str
    download_url: str
    creator: str | None
    license_name: str
    license_url: str | None
    attribution: str | None
    mime_type: str = "image/jpeg"
    width: int = 0
    height: int = 0
    media_type: str = "image"
    metadata: dict[str, Any] = field(default_factory=dict)
    relevance_score: float = 0.0
    rank_score: float = 0.0


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    extension: str
    mime_type: str
    width: int
    height: int
    focal_x: float
    focal_y: float


@dataclass(frozen=True)
class AssetSelection:
    candidate: AssetCandidate
    local_path: Path
    checksum: str
    mime_type: str
    file_size: int
    width: int
    height: int
    focal_x: float
    focal_y: float
    rank_score: float


@dataclass
class AssetRetrievalResult:
    selection: AssetSelection | None
    warnings: list[str] = field(default_factory=list)
    candidates_seen: int = 0


class AssetProvider(Protocol):
    name: str

    async def search(
        self, client: httpx.AsyncClient, brief: AssetBrief, limit: int
    ) -> list[AssetCandidate]: ...


def _clean_html(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = HTML_TAG.sub(" ", text)
    return " ".join(text.split())


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
        if token
        not in {
            "the",
            "and",
            "for",
            "with",
            "from",
            "that",
            "this",
            "news",
            "image",
            "photo",
            "file",
        }
    }


# Words that appear in media-library titles but say nothing about the subject.
# Matching on these produced confidently licensed, completely irrelevant images
# (a botanical survey figure over an election story), so they never count.
GENERIC_QUERY_TOKENS = {
    "about",
    "according",
    "after",
    "against",
    "america",
    "based",
    "before",
    "between",
    "called",
    "corroborated",
    "could",
    "during",
    "editorial",
    "footage",
    "general",
    "government",
    "independent",
    "information",
    "officials",
    "people",
    "photo",
    "photograph",
    "report",
    "reported",
    "reports",
    "results",
    "said",
    "says",
    "source",
    "sources",
    "stock",
    "story",
    "survey",
    "surveys",
    "video",
    "week",
    "while",
}


def license_allowed(name: str | None) -> bool:
    if not name:
        return False
    if BLOCKED_LICENSE.search(name) and not ALLOWED_LICENSE.search(name):
        return False
    return bool(ALLOWED_LICENSE.search(name))


def subject_match(candidate: AssetCandidate, brief: AssetBrief) -> bool:
    """True when the candidate text names at least one real subject of the scene."""

    subjects = {subject.lower() for subject in brief.subjects if len(subject) >= 3}
    if not subjects:
        return True
    text = _tokens(f"{candidate.title} {candidate.description}")
    return bool(subjects & text)


def relevance_score(candidate: AssetCandidate, brief: AssetBrief) -> float:
    query_tokens = (
        _tokens(brief.query) | {token.lower() for token in brief.keywords}
    ) - GENERIC_QUERY_TOKENS
    candidate_tokens = _tokens(f"{candidate.title} {candidate.description}")
    subjects = {subject.lower() for subject in brief.subjects if len(subject) >= 3}
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens & candidate_tokens) / len(query_tokens)
    title_overlap = len(query_tokens & _tokens(candidate.title)) / max(1, len(query_tokens))
    subject_overlap = len(subjects & candidate_tokens) / len(subjects) if subjects else 0.0
    return round(min(1.0, 0.5 * subject_overlap + 0.3 * overlap + 0.2 * title_overlap), 4)


def rank_candidate(candidate: AssetCandidate, brief: AssetBrief) -> float:
    relevance = relevance_score(candidate, brief)
    reliability = PROVIDER_RELIABILITY.get(candidate.provider, 0.6)
    width = candidate.width or 1280
    height = candidate.height or 720
    resolution = min(1.0, width / 1920) * 0.6 + min(1.0, height / 1080) * 0.4
    aspect = width / height if height else 1.0
    if brief.prefer_aspect == "9:16":
        aspect_fit = 1.0 if aspect < 0.9 else 0.55 if aspect < 1.2 else 0.35
    else:
        aspect_fit = 1.0 if aspect > 1.2 else 0.6 if aspect > 0.9 else 0.4
    license_score = 1.0 if license_allowed(candidate.license_name) else 0.0
    score = (
        0.42 * relevance
        + 0.18 * reliability
        + 0.18 * resolution
        + 0.12 * aspect_fit
        + 0.10 * license_score
    )
    return round(min(1.0, score), 4)


def sniff_image(data: bytes) -> tuple[str, str] | None:
    for signature, extension, mime_type in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return extension, mime_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def detect_focal_point(image: Image.Image) -> tuple[float, float]:
    """Find the busiest region instead of assuming the centre is important."""

    gray = image.convert("L").resize((96, 96), Image.Resampling.BILINEAR)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    pixels = list(edges.getdata())
    cells = [[0.0] * 3 for _ in range(3)]
    for y in range(96):
        for x in range(96):
            cells[min(2, y * 3 // 96)][min(2, x * 3 // 96)] += float(pixels[y * 96 + x])
    maximum = max(max(row) for row in cells) or 1.0
    best_row, best_column = max(
        ((row, column) for row in range(3) for column in range(3)),
        key=lambda position: cells[position[0]][position[1]],
    )
    # Weight the winning cell toward its centre while keeping a mild pull toward
    # the image centre, which avoids extreme crops on busy borders.
    focal_x = (best_column + 0.5) / 3
    focal_y = (best_row + 0.5) / 3
    focal_x = round(0.75 * focal_x + 0.25 * 0.5, 3)
    focal_y = round(0.75 * focal_y + 0.25 * 0.5, 3)
    if maximum <= 0:
        return 0.5, 0.5
    return min(0.92, max(0.08, focal_x)), min(0.92, max(0.08, focal_y))


def validate_image_bytes(
    data: bytes,
    *,
    min_width: int,
    min_height: int,
    max_bytes: int,
) -> ValidatedImage:
    if not data:
        raise AssetValidationError("Downloaded file was empty")
    if len(data) > max_bytes:
        raise AssetValidationError(
            f"Downloaded file is {len(data)} bytes; limit is {max_bytes} bytes"
        )
    signature = sniff_image(data)
    if signature is None:
        raise AssetValidationError("File signature is not a supported image format")
    extension, declared_mime = signature
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise AssetValidationError("Image pixel count exceeds the safety limit")
            if width < min_width or height < min_height:
                raise AssetValidationError(
                    f"Image is {width}x{height}; minimum is {min_width}x{min_height}"
                )
            focal_x, focal_y = detect_focal_point(image)
    except AssetValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AssetValidationError(f"Image could not be decoded: {exc}") from exc
    return ValidatedImage(
        data=data,
        extension=extension,
        mime_type=declared_mime,
        width=width,
        height=height,
        focal_x=focal_x,
        focal_y=focal_y,
    )


def normalize_image(validated: ValidatedImage, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(validated.data)) as image:
        image.seek(0)
        converted = image.convert("RGB")
        converted.save(output_path, "JPEG", quality=92, optimize=True)
    return output_path


class WikimediaCommonsProvider:
    name = "wikimedia"
    endpoint = "https://commons.wikimedia.org/w/api.php"

    async def search(
        self, client: httpx.AsyncClient, brief: AssetBrief, limit: int
    ) -> list[AssetCandidate]:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"{brief.query} filetype:bitmap",
            "gsrnamespace": 6,
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|mime|size|extmetadata",
            "iiurlwidth": 1800,
        }
        response = await client.get(self.endpoint, params=params)
        response.raise_for_status()
        payload = response.json()
        pages = payload.get("query", {}).get("pages", {})
        if isinstance(pages, list):
            page_values = pages
        else:
            page_values = list(pages.values())
        candidates: list[AssetCandidate] = []
        for page in page_values:
            info = (page.get("imageinfo") or [{}])[0]
            mime_type = info.get("mime", "")
            if not mime_type.startswith("image/"):
                continue
            metadata = info.get("extmetadata", {}) or {}
            license_name = _clean_html((metadata.get("LicenseShortName") or {}).get("value", ""))
            if not license_allowed(license_name):
                continue
            title = _clean_html(page.get("title", "")).removeprefix("File:")
            creator = _clean_html((metadata.get("Artist") or {}).get("value", "")) or None
            credit = _clean_html((metadata.get("Credit") or {}).get("value", ""))
            description = _clean_html((metadata.get("ImageDescription") or {}).get("value", ""))
            license_url = _clean_html((metadata.get("LicenseUrl") or {}).get("value", ""))
            source_url = info.get("descriptionurl") or (
                "https://commons.wikimedia.org/wiki/File:" + title.replace(" ", "_")
            )
            download_url = info.get("thumburl") or info.get("url")
            if not download_url:
                continue
            candidates.append(
                AssetCandidate(
                    provider=self.name,
                    provider_asset_id=str(page.get("pageid") or title),
                    title=title,
                    description=description,
                    source_url=source_url,
                    download_url=download_url,
                    creator=creator,
                    license_name=license_name,
                    license_url=license_url or None,
                    attribution=creator or credit or "Wikimedia Commons contributor",
                    mime_type=mime_type,
                    width=int(info.get("thumbwidth") or info.get("width") or 0),
                    height=int(info.get("thumbheight") or info.get("height") or 0),
                    metadata={
                        "credit": credit,
                        "license": license_name,
                        "description_url": source_url,
                    },
                )
            )
        return candidates


class NasaImageLibraryProvider:
    name = "nasa"
    endpoint = "https://images-api.nasa.gov/search"

    async def search(
        self, client: httpx.AsyncClient, brief: AssetBrief, limit: int
    ) -> list[AssetCandidate]:
        response = await client.get(
            self.endpoint,
            params={"q": brief.query, "media_type": "image", "page_size": limit},
        )
        response.raise_for_status()
        payload = response.json()
        candidates: list[AssetCandidate] = []
        for item in payload.get("collection", {}).get("items", []):
            data = (item.get("data") or [{}])[0]
            links = item.get("links") or []
            download_url = next((link.get("href") for link in links if link.get("href")), None)
            if not download_url:
                continue
            nasa_id = str(data.get("nasa_id") or "")
            if not nasa_id:
                continue
            candidates.append(
                AssetCandidate(
                    provider=self.name,
                    provider_asset_id=nasa_id,
                    title=_clean_html(data.get("title", nasa_id)),
                    description=_clean_html(data.get("description", "")),
                    source_url=f"https://images.nasa.gov/details/{nasa_id}",
                    download_url=download_url,
                    creator="NASA",
                    license_name="NASA Media Usage Guidelines",
                    license_url="https://www.nasa.gov/nasa-brand-center/images-and-media/",
                    attribution=f"NASA · {_clean_html(data.get('center', 'NASA'))}",
                    mime_type="image/jpeg",
                    width=0,
                    height=0,
                    metadata={
                        "date_created": data.get("date_created"),
                        "center": data.get("center"),
                        "keywords": data.get("keywords"),
                    },
                )
            )
        return candidates


class PexelsProvider:
    name = "pexels"
    endpoint = "https://api.pexels.com/v1/search"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(
        self, client: httpx.AsyncClient, brief: AssetBrief, limit: int
    ) -> list[AssetCandidate]:
        if not self._api_key:
            return []
        orientation = "portrait" if brief.prefer_aspect == "9:16" else "landscape"
        response = await client.get(
            self.endpoint,
            params={
                "query": brief.query,
                "per_page": limit,
                "orientation": orientation,
            },
            headers={"Authorization": self._api_key},
        )
        response.raise_for_status()
        payload = response.json()
        candidates: list[AssetCandidate] = []
        for photo in payload.get("photos", []):
            source = photo.get("src", {})
            download_url = source.get("large2x") or source.get("original")
            if not download_url:
                continue
            candidates.append(
                AssetCandidate(
                    provider=self.name,
                    provider_asset_id=str(photo.get("id")),
                    title=_clean_html(photo.get("alt", "Pexels photograph")),
                    description=_clean_html(photo.get("alt", "")),
                    source_url=photo.get("url", ""),
                    download_url=download_url,
                    creator=_clean_html(photo.get("photographer", "Pexels contributor")),
                    license_name="Pexels License",
                    license_url="https://www.pexels.com/license/",
                    attribution=f"{photo.get('photographer', 'Pexels')} · Pexels",
                    mime_type="image/jpeg",
                    width=int(photo.get("width") or 0),
                    height=int(photo.get("height") or 0),
                    metadata={"avg_color": photo.get("avg_color")},
                )
            )
        return candidates


class PixabayProvider:
    name = "pixabay"
    endpoint = "https://pixabay.com/api/"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(
        self, client: httpx.AsyncClient, brief: AssetBrief, limit: int
    ) -> list[AssetCandidate]:
        if not self._api_key:
            return []
        response = await client.get(
            self.endpoint,
            params={
                "key": self._api_key,
                "q": brief.query,
                "image_type": "photo",
                "per_page": max(3, min(20, limit)),
                "safesearch": "true",
            },
        )
        response.raise_for_status()
        payload = response.json()
        candidates: list[AssetCandidate] = []
        for photo in payload.get("hits", []):
            download_url = photo.get("largeImageURL") or photo.get("webformatURL")
            if not download_url:
                continue
            candidates.append(
                AssetCandidate(
                    provider=self.name,
                    provider_asset_id=str(photo.get("id")),
                    title=_clean_html(photo.get("tags", "Pixabay photograph")),
                    description=_clean_html(photo.get("tags", "")),
                    source_url=photo.get("pageURL", ""),
                    download_url=download_url,
                    creator=_clean_html(photo.get("user", "Pixabay contributor")),
                    license_name="Pixabay Content License",
                    license_url="https://pixabay.com/service/license-summary/",
                    attribution=f"{photo.get('user', 'Pixabay')} · Pixabay",
                    mime_type="image/jpeg",
                    width=int(photo.get("imageWidth") or 0),
                    height=int(photo.get("imageHeight") or 0),
                    metadata={"tags": photo.get("tags")},
                )
            )
        return candidates


class AssetPipeline:
    """Search, validate, cache, and rank licensed visual assets."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        providers: list[AssetProvider] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._providers = providers if providers is not None else self._build_providers(settings)

    def _build_providers(self, settings: Settings) -> list[AssetProvider]:
        available = {
            "wikimedia": WikimediaCommonsProvider(),
            "nasa": NasaImageLibraryProvider(),
            "pexels": PexelsProvider(settings.pexels_api_key),
            "pixabay": PixabayProvider(settings.pixabay_api_key),
        }
        priority = [
            name.strip().lower()
            for name in settings.asset_provider_priority.split(",")
            if name.strip()
        ]
        providers: list[AssetProvider] = []
        for name in priority:
            provider = available.get(name)
            if provider is not None:
                providers.append(provider)
        return providers

    async def _search(
        self,
        client: httpx.AsyncClient,
        brief: AssetBrief,
        *,
        limit: int,
    ) -> tuple[list[AssetCandidate], list[str]]:
        candidates: list[AssetCandidate] = []
        warnings: list[str] = []
        seen: set[str] = set()
        rejected_best = 0.0
        for provider in self._providers:
            try:
                results = await provider.search(client, brief, limit)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                warnings.append(f"{provider.name} search failed: {exc}")
                continue
            for candidate in results:
                if candidate.download_url in seen:
                    continue
                if not license_allowed(candidate.license_name):
                    continue
                candidate.relevance_score = relevance_score(candidate, brief)
                candidate.rank_score = rank_candidate(candidate, brief)
                seen.add(candidate.download_url)
                if (
                    candidate.relevance_score < self._settings.asset_min_relevance
                    or not subject_match(candidate, brief)
                ):
                    rejected_best = max(rejected_best, candidate.relevance_score)
                    continue
                candidates.append(candidate)
        candidates.sort(key=lambda item: item.rank_score, reverse=True)
        if not candidates and rejected_best:
            warnings.append(
                f"{len(seen)} licensed candidate(s) were rejected as off-subject "
                f"(best relevance {rejected_best:.2f}, minimum "
                f"{self._settings.asset_min_relevance:.2f})."
            )
        return candidates, warnings

    async def retrieve(
        self,
        brief: AssetBrief,
        *,
        strategy: str = "hybrid",
        aspect_ratio: str = "16:9",
    ) -> AssetRetrievalResult:
        if not self._settings.asset_enabled or strategy == "graphics_only":
            return AssetRetrievalResult(selection=None)
        brief = AssetBrief(
            scene_id=brief.scene_id,
            query=brief.query,
            keywords=brief.keywords,
            subjects=brief.subjects,
            description=brief.description,
            preferred_types=brief.preferred_types,
            prefer_aspect=aspect_ratio,
            min_width=brief.min_width,
            min_height=brief.min_height,
        )
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._settings.asset_download_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self._settings.asset_user_agent},
        )
        warnings: list[str] = []
        try:
            candidates, search_warnings = await self._search(
                client, brief, limit=max(3, self._settings.asset_requests_per_scene * 2)
            )
            warnings.extend(search_warnings)
            if not candidates:
                warnings.append("No licensed candidates were returned for this scene.")
                return AssetRetrievalResult(selection=None, warnings=warnings)
            for candidate in candidates[: self._settings.asset_requests_per_scene]:
                try:
                    selection = await self._download_and_validate(client, candidate)
                except (httpx.HTTPError, AssetValidationError, OSError, ValueError) as exc:
                    warnings.append(
                        f"{candidate.provider}:{candidate.provider_asset_id} rejected: {exc}"
                    )
                    continue
                return AssetRetrievalResult(
                    selection=selection,
                    warnings=warnings,
                    candidates_seen=len(candidates),
                )
            warnings.append("All ranked candidates failed validation.")
            return AssetRetrievalResult(
                selection=None, warnings=warnings, candidates_seen=len(candidates)
            )
        finally:
            if owns_client:
                await client.aclose()

    async def _download_and_validate(
        self, client: httpx.AsyncClient, candidate: AssetCandidate
    ) -> AssetSelection:
        response = await client.get(candidate.download_url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type and not (
            content_type.startswith("image/")
            or content_type in {"application/octet-stream", "binary/octet-stream"}
        ):
            raise AssetValidationError(f"Unexpected content type: {content_type}")
        data = response.content
        validated = validate_image_bytes(
            data,
            min_width=self._settings.asset_min_width,
            min_height=self._settings.asset_min_height,
            max_bytes=self._settings.asset_max_bytes,
        )
        checksum = hashlib.sha256(data).hexdigest()
        # The cache always stores a normalized JPEG so the extension matches the bytes.
        extension = "jpg"
        cache_path = self._settings.asset_cache_dir / checksum[:2] / f"{checksum}.{extension}"
        if not cache_path.is_file():
            normalize_image(validated, cache_path)
        return AssetSelection(
            candidate=candidate,
            local_path=cache_path,
            checksum=checksum,
            mime_type="image/jpeg",
            file_size=len(data),
            width=validated.width,
            height=validated.height,
            focal_x=validated.focal_x,
            focal_y=validated.focal_y,
            rank_score=candidate.rank_score,
        )
