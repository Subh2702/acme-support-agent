"""Vector retriever over the help-center collection."""

from __future__ import annotations

from pathlib import Path

import chromadb

from app.config import Settings
from app.rag.base import Passage
from app.rag.embeddings import Embedder, FastEmbedEmbedder, HashEmbedder
from app.rag.ingest import build_index, open_client


class ChromaRetriever:
    def __init__(
        self, collection: chromadb.Collection, embedder: Embedder, min_score: float = 0.0
    ) -> None:
        self._collection = collection
        self._embedder = embedder
        self._min_score = min_score

    def search(self, query: str, k: int = 4) -> list[Passage]:
        result = self._collection.query(
            query_embeddings=[self._embedder.embed_query(query)],  # type: ignore[list-item]
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )
        passages = []
        for source_id, doc, meta, dist in zip(
            result["ids"][0],
            result["documents"][0],
            result["metadatas"][0],  # type: ignore[index]
            result["distances"][0],
            strict=True,  # type: ignore[index]
        ):
            score = 1.0 - float(dist)
            if score >= self._min_score:
                passages.append(Passage(source_id, str(meta.get("title", "")), doc, score))
        return passages


def make_embedder(settings: Settings) -> Embedder:
    if settings.embedding_backend == "hash":
        return HashEmbedder()
    return FastEmbedEmbedder(settings.embedding_model, cache_dir=settings.fastembed_cache_dir)


def build_retriever(
    settings: Settings, *, persist_dir: Path | None = None, embedder: Embedder | None = None
) -> ChromaRetriever:
    embedder = embedder or make_embedder(settings)
    client = open_client(persist_dir)
    collection, _ = build_index(client, settings.kb_dir, embedder)
    return ChromaRetriever(collection, embedder)
