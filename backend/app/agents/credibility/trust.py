from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field


class TrustedSource(BaseModel):
    source_id: str
    trust_weight: float = Field(ge=0.0, le=1.0)
    domains: list[str] = Field(default_factory=list)


class TrustPolicyDocument(BaseModel):
    default_trust_weight: float = Field(default=0.35, ge=0.0, le=1.0)
    sources: list[TrustedSource] = Field(default_factory=list)


class SourceTrustPolicy:
    """Version-controlled editorial policy used as an input, never as ground truth."""

    def __init__(self, document: TrustPolicyDocument) -> None:
        self._default = document.default_trust_weight
        self._weights = {source.source_id: source.trust_weight for source in document.sources}
        self._domain_weights = {
            domain.lower().lstrip("."): source.trust_weight
            for source in document.sources
            for domain in source.domains
        }
        self._domain_sources: dict[str, set[str]] = {}
        for source in document.sources:
            for domain in source.domains:
                key = domain.lower().lstrip(".")
                self._domain_sources.setdefault(key, set()).add(source.source_id)
        self._publisher_roots = self._build_publisher_roots(document.sources)

    @staticmethod
    def _build_publisher_roots(sources: list[TrustedSource]) -> dict[str, str]:
        """Union sources that share a configured domain into one publisher group."""

        parent = {source.source_id: source.source_id for source in sources}

        def find(item: str) -> str:
            root = item
            while parent[root] != root:
                root = parent[root]
            while parent[item] != item:
                next_item = parent[item]
                parent[item] = root
                item = next_item
            return root

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        owner: dict[str, str] = {}
        for source in sources:
            for domain in source.domains:
                key = domain.lower().lstrip(".")
                existing = owner.get(key)
                if existing is None:
                    owner[key] = source.source_id
                else:
                    union(existing, source.source_id)
        return {source_id: find(source_id) for source_id in parent}

    def publisher_key(self, source_id: str) -> str | None:
        root = self._publisher_roots.get(source_id)
        return f"trust:{root}" if root else None

    def publisher_key_for_url(self, url: str) -> str | None:
        hostname = (urlparse(url).hostname or "").lower()
        if not hostname:
            return None
        roots: set[str] = set()
        for domain, source_ids in self._domain_sources.items():
            if hostname == domain or hostname.endswith(f".{domain}"):
                roots.update(self._publisher_roots[source_id] for source_id in source_ids)
        return f"trust:{min(roots)}" if roots else None

    @classmethod
    def from_file(cls, path: Path) -> SourceTrustPolicy:
        if not path.is_file():
            raise FileNotFoundError(f"Trust policy not found: {path}")
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(TrustPolicyDocument.model_validate(payload))

    def weight_for(self, source_id: str) -> float:
        return self._weights.get(source_id, self._default)

    def weight_for_url(self, url: str) -> float:
        hostname = (urlparse(url).hostname or "").lower()
        for domain, weight in self._domain_weights.items():
            if hostname == domain or hostname.endswith(f".{domain}"):
                return weight
        return self._default
