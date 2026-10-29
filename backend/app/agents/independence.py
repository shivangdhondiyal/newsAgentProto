from __future__ import annotations

import ipaddress
import re
from typing import Protocol
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Article, DuplicateCluster, DuplicateClusterMember


class PublisherResolver(Protocol):
    def publisher_key(self, source_id: str) -> str | None: ...

    def publisher_key_for_url(self, url: str) -> str | None: ...


# Common multi-label public suffixes. This is a deliberately small local list: the
# goal is publisher grouping for corroboration, not a full public-suffix database.
_MULTIPART_SUFFIXES = {
    "ac.uk",
    "co.in",
    "co.jp",
    "co.nz",
    "co.uk",
    "co.za",
    "com.au",
    "com.br",
    "com.cn",
    "com.mx",
    "com.sg",
    "com.tr",
    "com.tw",
    "gov.uk",
    "net.au",
    "net.uk",
    "org.au",
    "org.uk",
}


def _hostname(value: str | None) -> str:
    if not value:
        return ""
    try:
        host = (urlparse(value).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _registrable_domain(host: str) -> str:
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host
    parts = [part for part in host.split(".") if part]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _MULTIPART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _normalized_publisher_name(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip().casefold()
    normalized = re.split(r"\s+[—–]\s+|\s*\|\s*|\s*:\s*", normalized, maxsplit=1)[0]
    return normalized


def publisher_identity(
    source_id: str,
    publisher: str,
    url: str | None,
    canonical_url: str | None = None,
    trust_policy: PublisherResolver | None = None,
) -> str:
    """Return the publisher-level identity used for corroboration counting.

    Feeds such as BBC World and BBC Technology are separate ``source_id`` values but
    the same publisher. A trust policy groups configured sources that share domains;
    otherwise the article domain collapses subdomains to one publisher identity.
    """

    if trust_policy is not None:
        key = trust_policy.publisher_key(source_id)
        if key is None:
            key = trust_policy.publisher_key_for_url(canonical_url or url or "")
        if key:
            return key

    host = _hostname(canonical_url) or _hostname(url)
    if host:
        return f"domain:{_registrable_domain(host)}"
    name = _normalized_publisher_name(publisher)
    if name:
        return f"publisher:{name}"
    return f"source:{source_id}"


def publisher_identity_for_article(
    article: Article, trust_policy: PublisherResolver | None = None
) -> str:
    return publisher_identity(
        article.source_id,
        article.source_name,
        article.url,
        article.canonical_url,
        trust_policy,
    )


def filter_independent_publishers(
    article: Article,
    candidates: list[Article],
    trust_policy: PublisherResolver | None = None,
) -> list[Article]:
    """Keep at most one candidate per publisher, preserving the caller's order.

    Callers sort by relevance first, so the strongest report from a publisher survives.
    The target article's own publisher is excluded entirely.
    """

    seen = {publisher_identity_for_article(article, trust_policy)}
    independent: list[Article] = []
    for candidate in candidates:
        identity = publisher_identity_for_article(candidate, trust_policy)
        if identity in seen:
            continue
        seen.add(identity)
        independent.append(candidate)
    return independent


async def filter_independent_articles(
    session: AsyncSession, target_article_id: str, candidates: list[Article]
) -> list[Article]:
    """Remove syndicated copies so duplicate reports cannot fake corroboration.

    A ``confirmed`` or still-unresolved ``review`` cluster is treated conservatively as
    one original report. Only clusters a human explicitly rejected as duplicates are
    allowed to count as independent sources. The input order is preserved, so callers
    can sort by relevance before filtering and keep the strongest report per cluster.
    """

    if not candidates:
        return []

    article_ids = {target_article_id, *(candidate.id for candidate in candidates)}
    rows = (
        await session.execute(
            select(
                DuplicateClusterMember.article_id,
                DuplicateCluster.id,
                DuplicateCluster.status,
            )
            .join(DuplicateCluster, DuplicateCluster.id == DuplicateClusterMember.cluster_id)
            .where(DuplicateClusterMember.article_id.in_(article_ids))
        )
    ).all()

    memberships = {article_id: (cluster_id, status) for article_id, cluster_id, status in rows}
    target_clusters = {
        cluster_id
        for article_id, (cluster_id, status) in memberships.items()
        if article_id == target_article_id and status != "rejected"
    }

    seen_clusters: set[str] = set()
    independent: list[Article] = []
    for candidate in candidates:
        if candidate.id == target_article_id:
            continue
        membership = memberships.get(candidate.id)
        if membership is None:
            independent.append(candidate)
            continue
        cluster_id, cluster_status = membership
        if cluster_status == "rejected":
            independent.append(candidate)
            continue
        if cluster_id in target_clusters or cluster_id in seen_clusters:
            continue
        seen_clusters.add(cluster_id)
        independent.append(candidate)
    return independent
