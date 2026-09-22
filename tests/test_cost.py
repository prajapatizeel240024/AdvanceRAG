"""Cost accounting tests.

Cost is the part of this system a reviewer is most entitled to distrust, so the
arithmetic is pinned to numbers computed by hand from the published rates:

    claude-opus-5        $5.00 in / $25.00 out per 1M
                         cache read $0.50 (10%), cache write $6.25 (125%)
    gemini-embedding-001 $0.15 in per 1M

The database is the authority for live cost -- ``cost_ledger.cost_usd`` is a
generated column -- so these tests cover the estimator's arithmetic and the
Usage normalisation that feeds the ledger. The generated column and the budget
trigger are exercised in SQL, by ``scripts/verify_cost.sql``.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config.registry import ConfigBundle, ConfigFile, content_hash  # noqa: E402
from app.ingest.chunker import chunk_markdown  # noqa: E402
from app.ingest.estimator import _cost, estimate_ingestion  # noqa: E402
from app.providers.base import Usage  # noqa: E402

CORPUS = ROOT / "corpus" / "travel-policy-v1.md"


# ---------------------------------------------------------------- unit math --


def test_cost_formula():
    # 12,000 input tokens at $5.00/1M = $0.06
    assert _cost(12_000, Decimal("5.00")) == Decimal("0.06")
    # 400 output tokens at $25.00/1M = $0.01
    assert _cost(400, Decimal("25.00")) == Decimal("0.01")


def test_cache_read_is_one_tenth_of_input():
    """The saving that makes listwise reranking on Opus 5 affordable."""
    full = _cost(11_500, Decimal("5.00"))
    cached = _cost(11_500, Decimal("0.50"))
    assert cached == full / 10


def test_rerank_call_cached_vs_cold():
    """The measured figure quoted throughout this project.

    Cold:   12,000 in + 400 out            = $0.070000
    Cached: 500 in + 400 out + 11,500 read = $0.018250  (a 74% saving)
    """
    cold = _cost(12_000, Decimal("5.00")) + _cost(400, Decimal("25.00"))
    warm = (
        _cost(500, Decimal("5.00"))
        + _cost(400, Decimal("25.00"))
        + _cost(11_500, Decimal("0.50"))
    )
    assert cold == Decimal("0.070000")
    assert warm == Decimal("0.018250")
    assert warm / cold < Decimal("0.27")


# ------------------------------------------------------------------- usage --


def test_usage_addition():
    a = Usage(input_tokens=100, output_tokens=10, cache_read_tokens=5)
    b = Usage(input_tokens=200, output_tokens=20, cache_write_tokens=7)
    c = a + b
    assert c.input_tokens == 300
    assert c.output_tokens == 30
    assert c.cache_read_tokens == 5
    assert c.cache_write_tokens == 7


def test_cache_tokens_are_not_folded_into_input():
    """Folding them in would overstate a cached call roughly tenfold.

    They are priced at 10% (read) and 125% (write) of the input rate, so they
    have to reach the ledger as separate columns.
    """
    u = Usage(input_tokens=500, cache_read_tokens=11_500)
    assert u.input_tokens == 500
    assert u.cache_read_tokens == 11_500
    assert u.total == 12_000


# --------------------------------------------------------------- estimator --


def _bundle() -> ConfigBundle:
    """A bundle backed by the real config files on disk."""
    import yaml

    files = {}
    for name in ("models", "costs", "chunking", "retrieval"):
        raw = (ROOT / "config" / f"{name}.yaml").read_text()
        parsed = yaml.safe_load(raw)
        files[name] = ConfigFile(
            name=name,
            semver=str(parsed.get("version")),
            hash=content_hash(parsed),
            raw_text=raw,
            parsed=parsed,
        )
    return ConfigBundle(id="00000000-0000-0000-0000-000000000000", bundle_hash="x", files=files)


@pytest.fixture(scope="module")
def bundle():
    return _bundle()


@pytest.fixture(scope="module")
def chunks():
    return chunk_markdown(CORPUS.read_text(), "Northwind Global Travel Policy")


def test_embedding_is_the_cheap_part(bundle, chunks):
    """A finding worth stating plainly: embedding is not the cost driver.

    For this corpus, embedding is under a tenth of a cent. Contextualisation is
    ~400x more. Optimising embeddings would be optimising the wrong thing.
    """
    est = estimate_ingestion(chunks, CORPUS.read_text(), bundle)
    embed = next(li for li in est.line_items if li.label == "Embedding")
    ctx = next(li for li in est.line_items if li.label == "Contextualisation")
    assert embed.cost < Decimal("0.01")
    assert ctx.cost > embed.cost * 100


def test_reuse_collapses_reingestion_cost(bundle, chunks):
    """The annual-reissue case: 95% unchanged must cost far less than a full run."""
    full = estimate_ingestion(chunks, CORPUS.read_text(), bundle)
    already = {c.hash for c in chunks[: int(len(chunks) * 0.95)]}
    partial = estimate_ingestion(
        chunks, CORPUS.read_text(), bundle, already_embedded_hashes=already
    )
    assert partial.reused_chunk_count > 60
    assert partial.total_cost < full.total_cost * Decimal("0.2")


def test_unchanged_document_costs_nothing_to_reingest(bundle, chunks):
    already = {c.hash for c in chunks}
    est = estimate_ingestion(chunks, CORPUS.read_text(), bundle, already_embedded_hashes=already)
    assert est.new_chunk_count == 0
    assert est.total_cost == Decimal("0")


def test_estimate_marks_its_own_confidence(bundle, chunks):
    """A heuristic count must never be presented as exact."""
    est = estimate_ingestion(chunks, CORPUS.read_text(), bundle)
    assert est.token_confidence == "estimated"

    est2 = estimate_ingestion(
        chunks, CORPUS.read_text(), bundle, exact_counter=lambda t: len(t) // 4
    )
    assert est2.token_confidence == "exact"


def test_unpriced_model_is_refused(bundle, chunks):
    """An uncosted call must fail loudly rather than record zero."""
    from app.ingest.estimator import _price_for

    with pytest.raises(KeyError):
        _price_for(bundle.costs, "some-model-nobody-priced")
