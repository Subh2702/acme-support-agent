"""Knowledge-base ingestion: markdown articles -> section chunks -> Chroma.

Idempotent: the collection stores a hash of the KB content and the embedding
model name; if both match on startup, nothing is re-embedded.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.logging_setup import get_logger
from app.rag.embeddings import Embedder

log = get_logger(__name__)
COLLECTION = "help_center"


@dataclass(frozen=True)
class Chunk:
    source_id: str
    title: str
    section: str
    text: str


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def chunk_article(path: Path) -> list[Chunk]:
    """One chunk per `## ` section, prefixed with the article title for context."""
    content = path.read_text(encoding="utf-8")
    title_match = re.search(r"^# (.+)$", content, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem.replace("-", " ").title()
    chunks = []
    for match in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", content, re.MULTILINE | re.DOTALL):
        section, body = match.group(1).strip(), match.group(2).strip()
        if body:
            chunks.append(
                Chunk(
                    f"{path.stem}#{slugify(section)}",
                    title,
                    section,
                    f"{title} — {section}\n{body}",
                )
            )
    return chunks


def load_chunks(kb_dir: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(kb_dir.glob("*.md")):
        chunks.extend(chunk_article(path))
    return chunks


def kb_fingerprint(chunks: list[Chunk], embedder_name: str) -> str:
    digest = hashlib.sha256(embedder_name.encode())
    for c in chunks:
        digest.update(c.source_id.encode())
        digest.update(c.text.encode())
    return digest.hexdigest()[:32]


def open_client(persist_dir: Path | None) -> chromadb.ClientAPI:
    settings = ChromaSettings(anonymized_telemetry=False, allow_reset=True)
    if persist_dir is None:
        return chromadb.EphemeralClient(settings=settings)
    persist_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(persist_dir), settings=settings)


def build_index(
    client: chromadb.ClientAPI, kb_dir: Path, embedder: Embedder
) -> tuple[chromadb.Collection, bool]:
    """Ensure the collection matches the KB. Returns (collection, rebuilt?)."""
    chunks = load_chunks(kb_dir)
    if not chunks:
        raise RuntimeError(f"No knowledge-base articles found in {kb_dir}")
    fingerprint = kb_fingerprint(chunks, embedder.name)
    try:
        existing = client.get_collection(COLLECTION)
        if (existing.metadata or {}).get("fingerprint") == fingerprint:
            return existing, False
        client.delete_collection(COLLECTION)
    except Exception:  # collection missing (exception type varies across Chroma versions)
        log.info("creating knowledge-base collection")
    collection = client.create_collection(
        COLLECTION,
        metadata={"fingerprint": fingerprint, "hnsw:space": "cosine"},
        embedding_function=None,
    )
    collection.add(
        ids=[c.source_id for c in chunks],
        documents=[c.text for c in chunks],
        embeddings=embedder.embed_documents([c.text for c in chunks]),  # type: ignore[arg-type]
        metadatas=[{"title": c.title, "section": c.section} for c in chunks],
    )
    log.info("knowledge base indexed", extra={"chunks": len(chunks), "embedder": embedder.name})
    return collection, True
