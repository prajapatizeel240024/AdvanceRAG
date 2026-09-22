#!/usr/bin/env python
"""Run the 30-question evaluation set and record the results against a config bundle.

    scripts/evaluate.py                  run everything
    scripts/evaluate.py --category multi_hop
    scripts/evaluate.py --dry-run        show what would run, spend nothing

Results are written to `query_runs` like any other question, so "did config v3
beat v2" is a GROUP BY over `v_quality_by_config` rather than an afternoon of
spreadsheet work.

Two metrics are computed here rather than by an LLM judge:

  citation recall  — of the sections the answer was supposed to ground itself
                     in, how many it actually cited. This is the retrieval
                     metric that matters; an answer citing the wrong clause is
                     wrong even when its prose happens to be right.
  refusal accuracy — for the nine out-of-scope and underspecified questions,
                     whether the system declined instead of inventing an answer.
                     This is the metric a travel-policy assistant lives or dies
                     on, and it is exactly measurable.

Answer correctness is deliberately NOT scored automatically. Grading free text
needs a judge model, and a judge adds cost and a second source of error to a
run whose whole point is measuring the first one. The expected answers are in
the eval set for a human to read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402
import yaml  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "corpus" / "eval-set.yaml"
API = "http://127.0.0.1:8000"

SECTION_RE = re.compile(r"\b(\d+(?:\.\d+)+)\b")


async def ask(client: httpx.AsyncClient, question: str) -> dict:
    """Drive one question through the SSE endpoint and collect the result."""
    out = {
        "answer": "",
        "citations": [],
        "outcome": None,
        "cost": 0.0,
        "latency_ms": 0,
        "run_id": None,
        "error": None,
    }
    async with client.stream(
        "POST", f"{API}/api/ask", json={"question": question}, timeout=180
    ) as r:
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return out
        event = None
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event == "token":
                    out["answer"] += payload.get("text", "")
                elif event == "citations":
                    out["citations"] = payload
                elif event == "cost":
                    out["cost"] = payload.get("total_usd", 0.0)
                    out["latency_ms"] = payload.get("latency_ms", 0)
                elif event == "done":
                    out["outcome"] = payload.get("outcome")
                    out["run_id"] = payload.get("run_id")
                elif event == "error":
                    out["error"] = payload.get("message")
    return out


def cited_sections(citations: list[dict]) -> set[str]:
    """Pull section numbers out of the cited chunks' breadcrumbs."""
    found: set[str] = set()
    for c in citations:
        for m in SECTION_RE.finditer(c.get("breadcrumb", "")):
            found.add(m.group(1))
    return found


def score(q: dict, res: dict) -> dict:
    must = set(q.get("must_cite") or [])
    got = cited_sections(res["citations"])

    # Prefix match: citing 7.2 satisfies a requirement for 7, since the
    # subsection is where the answer actually lives.
    hit = {m for m in must if any(g == m or g.startswith(m + ".") for g in got)}
    recall = len(hit) / len(must) if must else None

    refused = (res["outcome"] or "").startswith("refused") or res["outcome"] == "needs_clarification"
    wants_refusal = bool(q.get("must_refuse")) or bool(q.get("requires_clarification"))
    refusal_ok = refused if wants_refusal else not refused

    return {
        "id": q["id"],
        "category": q["category"],
        "outcome": res["outcome"],
        "citation_recall": recall,
        "cited": sorted(got),
        "expected": sorted(must),
        "refusal_ok": refusal_ok,
        "wants_refusal": wants_refusal,
        "cost": res["cost"],
        "latency_ms": res["latency_ms"],
        "error": res["error"],
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category")
    ap.add_argument("--id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    spec = yaml.safe_load(EVAL.read_text())
    questions = spec["questions"]
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.id:
        questions = [q for q in questions if q["id"] == args.id]

    if args.dry_run:
        for q in questions:
            print(f"  {q['id']}  [{q['category']:<16}] {q['question']}")
        print(f"\n{len(questions)} questions. (--dry-run: nothing was run.)")
        return 0

    async with httpx.AsyncClient() as client:
        try:
            h = (await client.get(f"{API}/api/health", timeout=5)).json()
        except Exception:
            print(f"No API at {API}. Start it with: make api", file=sys.stderr)
            return 1
        if h.get("degraded"):
            print(
                f"API is in degraded mode ({', '.join(h['missing_keys'])}). "
                "Live answering needs credentials.",
                file=sys.stderr,
            )
            return 1

        results = []
        for q in questions:
            res = await ask(client, q["question"])
            s = score(q, res)
            results.append(s)
            mark = "ok " if s["refusal_ok"] and (s["citation_recall"] in (None, 1.0)) else "   "
            rec = "—" if s["citation_recall"] is None else f"{s['citation_recall']:.0%}"
            print(
                f"  {mark}{s['id']}  {s['category']:<16} recall {rec:>4}  "
                f"{(s['outcome'] or 'error'):<20} ${s['cost']:.5f}  {s['latency_ms']}ms"
            )
            if s["error"]:
                print(f"       error: {s['error']}")
            elif s["citation_recall"] not in (None, 1.0):
                print(f"       expected {s['expected']}, cited {s['cited']}")

    # ---- summary -----------------------------------------------------------
    by_cat: dict[str, list] = defaultdict(list)
    for s in results:
        by_cat[s["category"]].append(s)

    print(f"\n{'Category':<18} {'n':>3} {'citation recall':>16} {'refusal':>9}")
    print("-" * 50)
    for cat in sorted(by_cat):
        rows = by_cat[cat]
        recs = [r["citation_recall"] for r in rows if r["citation_recall"] is not None]
        rec = f"{sum(recs) / len(recs):.0%}" if recs else "—"
        ref = sum(1 for r in rows if r["refusal_ok"])
        print(f"{cat:<18} {len(rows):>3} {rec:>16} {ref:>4}/{len(rows):<4}")

    all_rec = [r["citation_recall"] for r in results if r["citation_recall"] is not None]
    total_cost = sum(r["cost"] for r in results)
    print("-" * 50)
    print(f"{'OVERALL':<18} {len(results):>3} "
          f"{(sum(all_rec) / len(all_rec) if all_rec else 0):>15.0%} "
          f"{sum(1 for r in results if r['refusal_ok']):>4}/{len(results):<4}")
    print(f"\ncost ${total_cost:.5f} over {len(results)} questions "
          f"(${total_cost / max(1, len(results)):.5f} each)")
    print(f"config bundle: {h['config_bundle']}")
    print("\nRuns are recorded in query_runs; compare configs with:")
    print("  psql -d travel_rag -c 'SELECT * FROM v_quality_by_config'")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
