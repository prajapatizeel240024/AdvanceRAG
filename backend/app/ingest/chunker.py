"""Structure-aware hierarchical chunking for policy documents.

Fixed-window chunking destroys a policy document, and it does so silently.
Split the per-diem table in section 7.2 at an arbitrary 600-token boundary and
one chunk holds the numbers 13, 18, 29, 10 with no column headers and no city
tier. Retrieval will still return it, the reranker will still like it, and the
model will still produce a confident wrong per diem. Nothing in the pipeline
downstream can recover the lost structure.

So the chunker follows the document's own structure:

  1. Split on heading boundaries first. A section shorter than the budget stays
     whole however short it is -- section 1.3 (topics governed by other
     policies) is under 100 tokens and is the single most important chunk for
     honest refusal.
  2. Subdivide oversized sections on paragraph boundaries, never mid-sentence.
  3. Treat tables and numbered clauses as atomic. A table that genuinely
     exceeds the budget is split by row with the header repeated into each
     part, and the parts are marked so retrieval can re-stitch them.
  4. Give every chunk a breadcrumb, prepended to the embedded text only, so
     citations show the policy's words rather than our scaffolding.

Token counting note: boundary decisions use a fast local estimate, because
calling a tokenizer API per candidate split would make ingestion unusable.
Cost estimation uses the provider's exact ``count_tokens`` -- see
``app.ingest.estimator``. The two are deliberately different: an approximate
boundary is harmless, an approximate bill is not.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

# Rough tokens-per-character for English prose across both Claude and Gemini
# tokenizers. Used only to decide where to split.
_CHARS_PER_TOKEN = 3.8

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+")
MONEY_RE = re.compile(r"\b(?:USD|GBP|EUR|INR)\s*([\d,]+(?:\.\d+)?)|\$\s*([\d,]+)")
CURRENCY_RE = re.compile(r"\b(?:USD|GBP|EUR|INR)\b")
# A bare numeric table cell. Bounded so years and clause numbers ("4.2") that
# happen to sit in a cell are not mistaken for amounts.
CELL_NUMBER_RE = re.compile(r"\d{2,6}(?:\.\d{1,2})?")
BAND_RE = re.compile(r"\bBand\s+(\d)\b")
TIER_RE = re.compile(r"\bTier\s+(\d)\b")

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "booking": ("book", "portal", "advance", "gateway", "fare"),
    "approvals": ("approval", "approver", "authorise", "department head", "sign-off"),
    "air_travel": ("flight", "cabin", "airline", "economy", "business class", "baggage"),
    "rail_travel": ("rail", "train", "first class", "season ticket"),
    "accommodation": ("hotel", "nightly", "room", "accommodation", "laundry"),
    "per_diem": ("per diem", "meal", "breakfast", "lunch", "dinner", "incidental"),
    "ground_transport": ("taxi", "ride-hail", "rental", "mileage", "parking", "toll"),
    "disruption_safety": ("disruption", "cancel", "delay", "emergency", "risk", "insurance"),
    "personal_travel": ("personal", "companion", "spouse", "leisure", "saturday"),
    "expense_claims": ("claim", "receipt", "reimburse", "submit", "corporate card"),
    "non_reimbursable": ("not reimbursable", "never reimbursable", "fine", "minibar"),
    "exceptions": ("exception", "override", "precedence", "conflict", "accommodation"),
    "governance": ("review", "policy owner", "governance", "annually"),
    "scope": ("scope", "applies to", "does not cover", "superseded"),
}


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _same_title(a: str, b: str) -> bool:
    """Loose title match, for deciding whether an H1 restates the document title.

    Compares alphanumerics only, so "Northwind Global -- Corporate Travel &
    Expense Policy" matches "Northwind Global Travel Policy" on its leading
    words without tripping over punctuation or an em dash.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).split()  # noqa: E731
    wa, wb = norm(a), norm(b)
    if not wa or not wb:
        return False
    overlap = len(set(wa) & set(wb))
    return overlap >= min(3, min(len(wa), len(wb)))


def normalise(text: str) -> str:
    """Normalise for hashing so trivial whitespace edits do not force re-embedding."""
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str, breadcrumb: str = "") -> str:
    return hashlib.sha256(
        f"{breadcrumb}\x00{normalise(text)}".encode("utf-8")
    ).hexdigest()


@dataclass
class Chunk:
    ordinal: int
    content: str
    breadcrumb: str
    section_path: list[str]
    section_title: str
    token_count: int
    contains_table: bool = False
    table_part: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    context_summary: str | None = None

    @property
    def hash(self) -> str:
        return content_hash(self.content, self.breadcrumb)

    def embed_text(self) -> str:
        """What actually gets embedded.

        Breadcrumb first, then the contextual summary if ingestion produced one,
        then the content. Kept apart from ``content`` so a citation renders the
        policy's own words.
        """
        parts = [self.breadcrumb]
        if self.context_summary:
            parts.append(self.context_summary)
        parts.append(self.content)
        return "\n\n".join(p for p in parts if p)


@dataclass
class _Section:
    level: int
    title: str
    number: str | None
    path: list[str]
    titles: list[str]
    lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines).strip()


def _parse_sections(markdown: str, doc_title: str) -> list[_Section]:
    """Walk the heading tree, accumulating body lines under each heading."""
    sections: list[_Section] = []
    stack: list[_Section] = []
    current: _Section | None = None

    for line in markdown.splitlines():
        m = HEADING_RE.match(line)
        if not m:
            if current is not None:
                current.lines.append(line)
            continue

        level = len(m.group(1))
        title = m.group(2).strip()
        cm = CLAUSE_RE.match(title)
        number = cm.group(1) if cm else None

        while stack and stack[-1].level >= level:
            stack.pop()

        path = [s.number for s in stack if s.number]
        titles = [s.title for s in stack]
        if number:
            path = path + [number]

        current = _Section(
            level=level,
            title=title,
            number=number,
            path=path,
            titles=titles + [title],
        )
        sections.append(current)
        stack.append(current)

    # A document whose level-1 heading is its title produces a first section
    # with no body; harmless, and dropped below.
    return [s for s in sections if s.text() or s.level <= 2]


def _split_blocks(text: str) -> list[tuple[str, str]]:
    """Split a section body into ``(kind, text)`` blocks.

    Tables are isolated as single blocks so they can be kept atomic. Everything
    else is a paragraph.
    """
    blocks: list[tuple[str, str]] = []
    buf: list[str] = []
    table: list[str] = []

    def flush_para() -> None:
        if buf and "".join(buf).strip():
            blocks.append(("para", "\n".join(buf).strip()))
        buf.clear()

    def flush_table() -> None:
        if table:
            blocks.append(("table", "\n".join(table).strip()))
        table.clear()

    for line in text.splitlines():
        if TABLE_ROW_RE.match(line):
            flush_para()
            table.append(line)
        else:
            if table:
                flush_table()
            if not line.strip():
                flush_para()
            else:
                buf.append(line)

    flush_table()
    flush_para()
    return blocks


def _split_table(table_text: str, max_tokens: int) -> list[str]:
    """Split an oversized table by row, repeating the header in every part.

    A table split without its header produces parts that look retrievable and
    are unanswerable -- four bare numbers with nothing saying what they measure.
    """
    lines = [ln for ln in table_text.splitlines() if ln.strip()]
    if not lines:
        return []

    header: list[str] = []
    body_start = 0
    if len(lines) >= 2 and TABLE_SEP_RE.match(lines[1]):
        header = lines[:2]
        body_start = 2

    body = lines[body_start:]
    if not body:
        return [table_text]

    parts: list[str] = []
    current = list(header)
    current_tokens = estimate_tokens("\n".join(header))

    for row in body:
        row_tokens = estimate_tokens(row)
        if current_tokens + row_tokens > max_tokens and len(current) > len(header):
            parts.append("\n".join(current))
            current = list(header)
            current_tokens = estimate_tokens("\n".join(header))
        current.append(row)
        current_tokens += row_tokens

    if len(current) > len(header):
        parts.append("\n".join(current))
    return parts or [table_text]


def _extract_metadata(text: str, section: _Section, has_table: bool) -> dict[str, Any]:
    lower = text.lower()

    values = {
        float(g.replace(",", ""))
        for m in MONEY_RE.finditer(text)
        for g in m.groups()
        if g
    }

    # Policy tables usually declare the currency once, in the column header
    # ("Nightly cap (USD)"), and leave the cells as bare numbers. Matching only
    # "USD 350" therefore extracts nothing from exactly the tables that carry
    # the caps and rates -- so when a table's header names a currency, treat its
    # numeric cells as monetary too.
    if has_table and CURRENCY_RE.search(text):
        for line in text.splitlines():
            if not TABLE_ROW_RE.match(line) or TABLE_SEP_RE.match(line):
                continue
            for cell in line.strip().strip("|").split("|"):
                cell = cell.strip().replace(",", "")
                if CELL_NUMBER_RE.fullmatch(cell):
                    values.add(float(cell))

    monetary = sorted(values)
    currencies = sorted(set(re.findall(r"\b(USD|GBP|EUR|INR)\b", text)))
    bands = sorted({f"Band {b}" for b in BAND_RE.findall(text)})
    tiers = sorted({f"Tier {t}" for t in TIER_RE.findall(text)})
    clauses = sorted(set(re.findall(r"\bsection\s+(\d+(?:\.\d+)*)", lower)))

    # Score topics over the section title as well as the body -- the title is
    # the strongest single signal and is short enough not to dilute the match.
    haystack = f"{section.title.lower()} {lower}"
    scored = {
        topic: sum(haystack.count(kw) for kw in kws)
        for topic, kws in TOPIC_KEYWORDS.items()
    }
    best = max(scored, key=lambda k: scored[k]) if any(scored.values()) else "scope"

    meta: dict[str, Any] = {
        "topic": best,
        "section_number": section.number,
        "section_title": section.title,
        "contains_table": has_table,
    }
    if monetary:
        meta["monetary_values"] = monetary
    if currencies:
        meta["currencies"] = currencies
    if bands:
        meta["grade_bands"] = bands
    if tiers:
        meta["city_tiers"] = tiers
    if clauses:
        meta["references"] = clauses
    return meta


def chunk_markdown(
    markdown: str,
    doc_title: str,
    max_tokens: int = 600,
    min_tokens: int = 60,
    overlap_tokens: int = 90,
    table_hard_cap_tokens: int = 1200,
    breadcrumb_separator: str = " > ",
) -> list[Chunk]:
    """Chunk a markdown policy document structurally."""
    sections = _parse_sections(markdown, doc_title)
    chunks: list[Chunk] = []
    ordinal = 0

    for section in sections:
        body = section.text()
        if not body:
            continue

        # The document's own H1 is its title, so including both would render
        # every breadcrumb as "Title > Title > 4. Air Travel".
        titles = list(section.titles)
        if titles and section.level >= 1:
            first = titles[0]
            if first == doc_title or _same_title(first, doc_title):
                titles = titles[1:]
        breadcrumb = breadcrumb_separator.join([doc_title, *titles])
        blocks = _split_blocks(body)

        # Accumulate blocks up to the budget, flushing on overflow. Tables are
        # never merged into a mixed chunk -- they flush what is pending and
        # stand alone, so a table is always retrievable as a table.
        pending: list[str] = []
        pending_tokens = 0

        def flush(part: int | None = None, is_table: bool = False) -> None:
            nonlocal pending, pending_tokens, ordinal
            if not pending:
                return
            text = "\n\n".join(pending).strip()
            if not text:
                pending, pending_tokens = [], 0
                return
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    content=text,
                    breadcrumb=breadcrumb,
                    section_path=section.path,
                    section_title=section.title,
                    token_count=estimate_tokens(text),
                    contains_table=is_table,
                    table_part=part,
                    metadata=_extract_metadata(text, section, is_table),
                )
            )
            ordinal += 1
            pending, pending_tokens = [], 0

        for kind, text in blocks:
            tokens = estimate_tokens(text)

            if kind == "table":
                flush()
                if tokens <= table_hard_cap_tokens:
                    pending = [text]
                    pending_tokens = tokens
                    flush(is_table=True)
                else:
                    for i, part in enumerate(_split_table(text, max_tokens), start=1):
                        pending = [part]
                        pending_tokens = estimate_tokens(part)
                        flush(part=i, is_table=True)
                continue

            if pending_tokens + tokens > max_tokens and pending:
                tail = pending[-1] if overlap_tokens else None
                flush()
                # Carry the previous paragraph forward so a rule split across a
                # boundary keeps its antecedent ("it", "this cap", "the above").
                if tail and estimate_tokens(tail) <= overlap_tokens:
                    pending = [tail]
                    pending_tokens = estimate_tokens(tail)

            pending.append(text)
            pending_tokens += tokens

        flush()

    # Merge runt chunks forward. A 20-token fragment is not independently
    # retrievable and only dilutes the index -- unless it is a table, which is
    # meaningful at any size.
    merged: list[Chunk] = []
    for c in chunks:
        if (
            merged
            and c.token_count < min_tokens
            and not c.contains_table
            and not merged[-1].contains_table
            and merged[-1].breadcrumb == c.breadcrumb
            and merged[-1].token_count + c.token_count <= max_tokens
        ):
            prev = merged[-1]
            prev.content = f"{prev.content}\n\n{c.content}"
            prev.token_count = estimate_tokens(prev.content)
            continue
        merged.append(c)

    for i, c in enumerate(merged):
        c.ordinal = i
    return merged
