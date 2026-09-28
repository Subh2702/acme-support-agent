"""RAG: chunking, idempotent indexing, retrieval quality."""

from pathlib import Path

import pytest

from app.config import ROOT_DIR
from app.rag.embeddings import HashEmbedder
from app.rag.ingest import build_index, chunk_article, kb_fingerprint, load_chunks, open_client
from app.rag.retriever import ChromaRetriever

KB = ROOT_DIR / "app" / "rag" / "kb"

# (question, expected source article) — includes Hinglish and paraphrases
GOLDEN = [
    ("how many days do I have to return an item", "returns-and-refunds"),
    ("COD refund bank details", "cash-on-delivery"),
    ("UPI refund kitne din mein aata hai", "returns-and-refunds"),
    ("can I add my GSTIN after the invoice is generated", "gst-invoices"),
    ("do you deliver to Leh or ship abroad", "international-and-serviceability"),
    ("parcel shows delivered but I never got it", "order-not-received"),
    ("can I change the delivery address after it shipped", "change-delivery-address"),
    ("my tent pole arrived broken", "damaged-or-wrong-item"),
    ("is there no cost EMI on credit card", "payment-methods"),
    ("how do I cancel before shipping", "cancellations"),
    ("warranty on trekking shoes sole", "warranty"),
    ("someone called asking for OTP to process refund", "fraud-and-privacy"),
]


def test_kb_has_15_to_25_articles_with_indian_context() -> None:
    articles = sorted(KB.glob("*.md"))
    assert 15 <= len(articles) <= 25
    text = " ".join(p.read_text() for p in articles)
    for term in ("UPI", "COD", "GST", "PIN code", "Delhivery", "NEFT", "₹"):
        assert term in text
    assert "$" not in text


def test_kb_never_mentions_internal_approval_thresholds() -> None:
    text = " ".join(p.read_text() for p in KB.glob("*.md"))
    assert "₹2,000" not in text and "₹10,000" not in text


def test_chunking_by_section(tmp_path: Path) -> None:
    (tmp_path / "demo-article.md").write_text(
        "# Demo Article\n\n## First part\nAlpha text.\n\n## Second Part!\nBeta text.\n"
    )
    chunks = chunk_article(tmp_path / "demo-article.md")
    assert [c.source_id for c in chunks] == ["demo-article#first-part", "demo-article#second-part"]
    assert chunks[0].text.startswith("Demo Article — First part")
    assert chunks[1].title == "Demo Article"


def test_every_kb_chunk_has_unique_id() -> None:
    chunks = load_chunks(KB)
    ids = [c.source_id for c in chunks]
    assert len(ids) == len(set(ids)) and len(ids) > 40


def test_index_is_idempotent_and_rebuilds_on_change(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text("# A\n\n## One\nReturns within 30 days.\n")
    client = open_client(tmp_path / "chroma")
    embedder = HashEmbedder()
    _, rebuilt = build_index(client, kb, embedder)
    assert rebuilt
    _, rebuilt = build_index(client, kb, embedder)
    assert not rebuilt
    (kb / "b.md").write_text("# B\n\n## Two\nShipping is free above ₹999.\n")
    collection, rebuilt = build_index(client, kb, embedder)
    assert rebuilt and collection.count() == 2
    # a fresh client on the same directory sees the persisted index
    _, rebuilt = build_index(open_client(tmp_path / "chroma"), kb, embedder)
    assert not rebuilt


def test_fingerprint_depends_on_embedder() -> None:
    chunks = load_chunks(KB)
    assert kb_fingerprint(chunks, "a") != kb_fingerprint(chunks, "b")


def test_empty_kb_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        build_index(open_client(None), tmp_path, HashEmbedder())


def _hit_rate(retriever: ChromaRetriever, k: int = 3) -> float:
    hits = 0
    for question, article in GOLDEN:
        sources = [p.source_id.split("#")[0] for p in retriever.search(question, k)]
        hits += article in sources
    return hits / len(GOLDEN)


def test_retrieval_with_hash_embedder_is_reasonable() -> None:
    embedder = HashEmbedder()
    collection, _ = build_index(open_client(None), KB, embedder)
    retriever = ChromaRetriever(collection, embedder)
    top = retriever.search("return window 30 days", 2)
    assert top[0].source_id.startswith("returns-and-refunds")
    assert _hit_rate(retriever) >= 0.5


def _fastembed_cached() -> bool:
    return (ROOT_DIR / "data" / "models").exists() and any((ROOT_DIR / "data" / "models").iterdir())


@pytest.mark.skipif(not _fastembed_cached(), reason="fastembed model not downloaded locally")
def test_retrieval_quality_with_real_embedder() -> None:
    from app.rag.embeddings import FastEmbedEmbedder

    embedder = FastEmbedEmbedder("BAAI/bge-small-en-v1.5", cache_dir=ROOT_DIR / "data" / "models")
    collection, _ = build_index(open_client(None), KB, embedder)
    assert _hit_rate(ChromaRetriever(collection, embedder)) >= 0.9
