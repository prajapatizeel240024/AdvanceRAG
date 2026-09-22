#!/usr/bin/env python
"""Seed the default tenant, user and budgets, and register the config bundle.

Idempotent -- safe to run repeatedly. The system is single-user today, so this
creates the one tenant and one user that exist; the schema is multi-tenant
throughout, so adding a second is an INSERT rather than a migration.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import asyncpg  # noqa: E402

from app.config.registry import register_bundle  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.graph.resolver import RoleResolver  # noqa: E402


async def main() -> int:
    s = get_settings()
    dsn = s.database_url.replace("postgresql://rag_app@", "postgresql://")
    conn = await asyncpg.connect(dsn)

    tenant_id = await conn.fetchval(
        """
        WITH ins AS (
            INSERT INTO tenants (slug, name) VALUES ($1, $2)
            ON CONFLICT (slug) DO NOTHING RETURNING id
        )
        SELECT id FROM ins UNION ALL SELECT id FROM tenants WHERE slug=$1 LIMIT 1
        """,
        s.default_tenant_slug,
        s.default_tenant_slug.title(),
    )

    user_id = await conn.fetchval(
        """
        WITH ins AS (
            INSERT INTO users (tenant_id, email, display_name, role)
            VALUES ($1, $2, $3, 'admin')
            ON CONFLICT DO NOTHING RETURNING id
        )
        SELECT id FROM ins
        UNION ALL
        SELECT id FROM users WHERE tenant_id=$1 AND lower(email)=lower($2)
        LIMIT 1
        """,
        tenant_id,
        s.default_user_email,
        s.default_user_email.split("@")[0],
    )

    bundle = await register_bundle(conn, s.config_dir, label="bootstrap")
    resolver = RoleResolver(bundle, s)
    await resolver.register_all(conn)

    # Budgets come from costs.yaml, so the guardrail and the documented figure
    # cannot drift apart.
    budgets = bundle.costs.get("budgets", {})
    await conn.execute(
        """
        INSERT INTO tenant_budgets (tenant_id, per_user_daily_usd,
                                    per_user_monthly_usd, per_tenant_monthly_usd, on_exceed)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (tenant_id) DO UPDATE
            SET per_user_daily_usd = EXCLUDED.per_user_daily_usd,
                per_user_monthly_usd = EXCLUDED.per_user_monthly_usd,
                per_tenant_monthly_usd = EXCLUDED.per_tenant_monthly_usd,
                on_exceed = EXCLUDED.on_exceed,
                updated_at = now()
        """,
        tenant_id,
        budgets.get("per_user_daily_usd", 5.00),
        budgets.get("per_user_monthly_usd", 50.00),
        budgets.get("per_tenant_monthly_usd", 500.00),
        budgets.get("on_exceed", "reject"),
    )

    print(f"tenant   {s.default_tenant_slug}  {tenant_id}")
    print(f"user     {s.default_user_email}  {user_id}")
    print(f"bundle   {bundle.bundle_hash[:16]}  {bundle.id}")
    print(f"config   {len(bundle.files)} files registered")
    missing = s.missing_keys()
    if missing:
        print(f"\nDEGRADED MODE: {', '.join(missing)} not set.")
        print("  Estimates, schema and provenance work. Live answering does not.")
    await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
