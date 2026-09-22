#!/usr/bin/env python
"""Ingest the corpus from the command line.

    scripts/ingest.py --estimate-only   price it, spend nothing, no keys needed
    scripts/ingest.py                   estimate, then ingest
    scripts/ingest.py --no-context      skip contextualisation (far cheaper)

The estimate always runs first and always prints, even when ingesting, so the
cost is on screen before anything is spent rather than after.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import asyncpg  # noqa: E402

from app.config.registry import register_bundle  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.graph.resolver import RoleResolver  # noqa: E402
from app.ingest.pipeline import Ingestor  # noqa: E402
from app.providers.base import MissingCredentialError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def print_estimate(est) -> None:
    d = est.as_dict()
    print(f"\n  {'Line item':<20} {'Model':<24} {'Tokens':>10}  {'Cost':>12}")
    print(f"  {'-' * 20} {'-' * 24} {'-' * 10}  {'-' * 12}")
    for li in d["line_items"]:
        print(
            f"  {li['label']:<20} {li['model_id']:<24} {li['tokens']:>10,}  "
            f"${li['cost_usd']:>11.6f}"
        )
        if li["note"]:
            print(f"  {'':<20} {li['note']}")
    print(f"  {'-' * 20} {'-' * 24} {'-' * 10}  {'-' * 12}")
    print(f"  {'TOTAL':<20} {'':<24} {'':>10}  ${d['total_cost_usd']:>11.6f}")
    print(
        f"\n  {d['chunk_count']} chunks"
        + (f", {d['new_chunk_count']} new" if d["reused_chunk_count"] else "")
        + (
            f", {d['reused_chunk_count']} reused "
            f"(saving ${d['reuse_saving_usd']:.6f})"
            if d["reused_chunk_count"]
            else ""
        )
    )
    if d["token_confidence"] == "estimated":
        print("  Token counts are heuristic (no API key for exact counting).")
    for w in d["warnings"]:
        print(f"  WARNING: {w}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="corpus/travel-policy-v1.md")
    ap.add_argument("--slug", default="travel-policy")
    ap.add_argument("--title", default="Northwind Global Travel Policy")
    ap.add_argument("--version", default="4.2")
    ap.add_argument("--estimate-only", action="store_true")
    ap.add_argument(
        "--no-context",
        action="store_true",
        help="Skip contextualisation. Cuts cost by ~99% and retrieval quality somewhat.",
    )
    args = ap.parse_args()

    s = get_settings()
    dsn = s.database_url.replace("postgresql://rag_app@", "postgresql://")
    conn = await asyncpg.connect(dsn)

    bundle = await register_bundle(conn, s.config_dir, label=s.app_env)
    resolver = RoleResolver(bundle, s)
    await resolver.register_all(conn)

    row = await conn.fetchrow(
        "SELECT * FROM fn_bootstrap_principal($1)", s.default_tenant_slug
    )
    if row is None:
        print("No seeded tenant. Run: make seed", file=sys.stderr)
        return 1

    await conn.execute("SELECT set_config('app.tenant_id', $1, false)", str(row["tenant_id"]))
    await conn.execute("SELECT set_config('app.user_id', $1, false)", str(row["user_id"]))

    ing = Ingestor(
        conn=conn,
        bundle=bundle,
        resolver=resolver,
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
    )

    path = ROOT / args.path
    print(f"Estimating {args.path} …")
    run_id, dv_id, chunks, est, raw = await ing.prepare(
        path, args.slug, args.title, args.version
    )
    print_estimate(est)

    if args.estimate_only:
        print("\n(--estimate-only: nothing was spent.)")
        await conn.close()
        return 0

    cap = float(bundle.costs.get("budgets", {}).get("per_ingestion_run_usd", 2.00))
    if float(est.total_cost) > cap:
        print(
            f"\nRefusing: ${est.total_cost:.4f} exceeds the per-run cap of ${cap:.2f} "
            "(budgets.per_ingestion_run_usd in config/costs.yaml).",
            file=sys.stderr,
        )
        await conn.close()
        return 2

    print("\nIngesting …")
    try:
        res = await ing.run(
            run_id, dv_id, chunks, raw, contextualise=False if args.no_context else None
        )
    except MissingCredentialError as exc:
        print(f"\n{exc}", file=sys.stderr)
        await conn.execute(
            "UPDATE ingestion_runs SET status='failed', error=$2 WHERE id=$1",
            run_id,
            str(exc),
        )
        await conn.close()
        return 3

    print(
        f"  {res.chunk_count} chunks — {res.embedded_count} embedded, "
        f"{res.reused_count} reused"
    )
    print(
        f"  estimated ${float(est.total_cost):.6f}   actual ${res.actual_cost:.6f}"
    )
    await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
