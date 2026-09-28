"""Embedding backends.

`FastEmbedEmbedder` (ONNX, no API key) is used in production. `HashEmbedder` is a
deterministic feature-hashing bag-of-words used in unit tests and CI so they never
download a model.
"""

from __future__ import annotations

import hashlib
import math
import re
from itertools import pairwise
from pathlib import Path
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9₹]+")


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashEmbedder:
    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = f"hash-{dim}"

    def _vector(self, text: str) -> list[float]:
        tokens = _TOKEN_RE.findall(text.lower())
        features = tokens + [f"{a}_{b}" for a, b in pairwise(tokens)]
        vec = [0.0] * self.dim
        for feat in features:
            digest = hashlib.md5(feat.encode(), usedforsecurity=False).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            vec[idx] += 1.0 if digest[4] % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FastEmbedEmbedder:
    def __init__(self, model_name: str, cache_dir: Path | None = None) -> None:
        from fastembed import TextEmbedding

        self.name = model_name
        self._model = TextEmbedding(
            model_name=model_name, cache_dir=str(cache_dir) if cache_dir else None
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()
