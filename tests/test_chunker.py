"""Chunker tests.

These target the specific failure this project is built to prevent: a table
split away from its headers, retrieved confidently, and turned into a wrong
per-diem figure. Every assertion below corresponds to a way that can happen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ingest.chunker import (  # noqa: E402
    chunk_markdown,
    content_hash,
    estimate_tokens,
    normalise,
)

CORPUS = ROOT / "corpus" / "travel-policy-v1.md"
TITLE = "Northwind Global Travel Policy"


@pytest.fixture(scope="module")
def chunks():
    return chunk_markdown(CORPUS.read_text(encoding="utf-8"), TITLE)


def test_produces_chunks(chunks):
    assert len(chunks) > 20


def test_no_non_table_chunk_exceeds_budget(chunks):
    """Budget overruns push relevant text out of the generation context."""
    over = [c for c in chunks if c.token_count > 600 and not c.contains_table]
    assert not over, [c.breadcrumb for c in over]


def test_per_diem_table_keeps_its_header(chunks):
    """The central regression test.

    Section 7.2 is a rate table. Split from its header row, a chunk reading
    "| Tier 3 | 13 | 18 | 29 | 10 | 70 |" is unanswerable but still perfectly
    retrievable -- which is how a system produces a confident wrong per diem.
    """
    table = next(
        (c for c in chunks if "Incidentals" in c.content and c.contains_table), None
    )
    assert table is not None, "per-diem table was not kept as a table chunk"
    assert "City tier" in table.content
    assert "Breakfast" in table.content
    # All four tiers must be in one chunk, or a tier lookup can miss.
    for tier in ("Tier 1", "Tier 2", "Tier 3", "Tier 4"):
        assert tier in table.content
    assert "110" in table.content and "55" in table.content


def test_cabin_class_matrix_intact(chunks):
    """Cabin class depends on duration AND grade; the matrix must stay whole."""
    m = next(
        (c for c in chunks if "Premium Economy" in c.content and c.contains_table), None
    )
    assert m is not None
    assert "Band 1–2" in m.content and "Band 6" in m.content
    assert "Under 4 hours" in m.content and "Over 10 hours" in m.content


def test_out_of_scope_section_survives(chunks):
    """Section 1.3 is short but is the basis of every honest refusal."""
    c = next(
        (c for c in chunks if "Relocation" in c.content and "Global Mobility" in c.content),
        None,
    )
    assert c is not None, "section 1.3 was lost; refusals would have nothing to cite"


def test_breadcrumbs_do_not_duplicate_title(chunks):
    for c in chunks:
        head = c.breadcrumb.split(" > ")
        assert head.count(TITLE) <= 1, c.breadcrumb


def test_breadcrumb_prefixed_only_in_embed_text(chunks):
    """Citations must show the policy's words, not our scaffolding."""
    c = next(c for c in chunks if c.breadcrumb and c.section_title)
    assert c.breadcrumb in c.embed_text()
    assert not c.content.startswith(c.breadcrumb)


def test_metadata_extraction(chunks):
    per_diem = next(c for c in chunks if "Incidentals" in c.content and c.contains_table)
    assert per_diem.metadata["topic"] == "per_diem"
    assert per_diem.metadata["contains_table"] is True
    assert "city_tiers" in per_diem.metadata

    caps = next(c for c in chunks if "Nightly cap" in c.content)
    assert 350.0 in caps.metadata.get("monetary_values", [])


def test_hash_is_stable_under_whitespace_only_edits():
    """Reuse depends on this: cosmetic edits must not trigger re-embedding."""
    a = content_hash("The per diem is  USD 110.\n", "X > Y")
    b = content_hash("The per diem is USD 110.", "X > Y")
    assert a == b


def test_hash_changes_when_content_changes():
    assert content_hash("USD 110", "X") != content_hash("USD 120", "X")


def test_hash_includes_breadcrumb():
    """Identical text under different headings is different content."""
    assert content_hash("Standard", "4.2 Cabin") != content_hash("Standard", "5.2 Rail")


def test_reingestion_is_mostly_reuse(chunks):
    """A small edit must not re-embed the document.

    This is the property the ingestion cost story rests on: a policy reissue
    touching a few lines should cost a few percent of a full ingest.
    """
    text = CORPUS.read_text(encoding="utf-8")
    edited = text.replace(
        "Airport parking is reimbursable for trips up to **seven days**",
        "Airport parking is reimbursable for trips up to **ten days**",
    )
    assert edited != text

    before = {c.hash for c in chunks}
    after = chunk_markdown(edited, TITLE)
    changed = [c for c in after if c.hash not in before]
    assert len(changed) <= 3, f"{len(changed)} chunks changed for a one-line edit"


def test_table_split_repeats_header():
    """An oversized table splits by row and repeats the header into each part."""
    rows = "\n".join(f"| City {i} | {i * 10} | {i * 5} |" for i in range(200))
    md = f"# Doc\n\n## 1. Rates\n\n| City | Cap | Meal |\n|---|---|---|\n{rows}\n"
    out = chunk_markdown(md, "Doc", max_tokens=200, table_hard_cap_tokens=300)
    parts = [c for c in out if c.table_part]
    assert len(parts) > 1
    for p in parts:
        assert "| City | Cap | Meal |" in p.content


def test_normalise_collapses_whitespace():
    assert normalise("a  b\n\nc ") == "a b c"


def test_estimate_tokens_monotonic():
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 100)
