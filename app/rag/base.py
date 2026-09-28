"""Retriever interface shared by the tools layer and the RAG implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Passage:
    source_id: str  # "<article-slug>#<section-slug>"
    title: str
    text: str
    score: float


class Retriever(Protocol):
    def search(self, query: str, k: int = 4) -> list[Passage]: ...
