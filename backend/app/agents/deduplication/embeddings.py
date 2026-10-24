from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol


class EmbeddingProvider(Protocol):
    """Replaceable local embedding boundary; no agent depends on a model SDK directly."""

    @property
    def model_name(self) -> str: ...

    def embed_documents(self, documents: Sequence[str]) -> list[list[float]]: ...


class SentenceTransformerEmbeddingProvider:
    """Local SentenceTransformers provider, loaded only when a deduplication run begins."""

    def __init__(self, model_name: str, batch_size: int) -> None:
        self._model_name = model_name
        self._batch_size = batch_size
        self._model = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is not installed; install the project dependencies first"
                ) from exc
            self._model = SentenceTransformer(self._model_name)
        embeddings = self._model.encode(
            list(documents),
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [vector.tolist() for vector in embeddings]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity while safely rejecting incompatible stored vectors."""

    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)
