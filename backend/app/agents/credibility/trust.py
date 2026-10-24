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
