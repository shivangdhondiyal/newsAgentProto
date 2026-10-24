from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Article, DuplicateCluster, DuplicateClusterMember


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

    memberships = {
        article_id: (cluster_id, status) for article_id, cluster_id, status in rows
    }
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
