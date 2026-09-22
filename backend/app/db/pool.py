"""Connection pool and tenant-scoped transactions.

The one genuinely subtle thing in this module is ``SET LOCAL``.

Row-level security reads the tenant from a session GUC. Connections come from a
pool and are handed to unrelated requests afterwards, so a plain ``SET`` would
leave the previous request's tenant id on the connection -- and the next request
to forget to set it would silently read another tenant's data. ``SET LOCAL``
scopes the value to the enclosing transaction and is reset on commit or
rollback, which makes the leak structurally impossible rather than merely
unlikely.

Every tenant-scoped query therefore runs inside ``tenant_tx()``. There is no
supported way to query tenant data outside one.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from uuid import UUID

import asyncpg

from app.core.settings import get_settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        s = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=s.database_url,
            min_size=s.db_pool_min,
            max_size=s.db_pool_max,
            # pgvector sends vectors as text over the wire unless a codec is
            # registered. Registering it once per connection keeps the call
            # sites free of manual "[1,2,3]" string formatting.
            init=_register_codecs,
        )
    return _pool


async def _register_codecs(conn: asyncpg.Connection) -> None:
    from pgvector.asyncpg import register_vector

    try:
        await register_vector(conn)
    except Exception:
        # The extension may not exist yet during the very first migration run.
        # Vector queries would fail later anyway, and far more informatively.
        pass


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        return await init_pool()
    return _pool


@asynccontextmanager
async def tenant_tx(
    tenant_id: UUID | str,
    user_id: UUID | str | None = None,
) -> AsyncIterator[asyncpg.Connection]:
    """Open a transaction with the RLS context set for its duration.

    ``set_config(..., is_local => true)`` is the function form of ``SET LOCAL``
    and, unlike the statement form, accepts a bind parameter -- so the tenant id
    is passed as a parameter rather than interpolated into SQL.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            if user_id is not None:
                await conn.execute(
                    "SELECT set_config('app.user_id', $1, true)", str(user_id)
                )
            yield conn


@asynccontextmanager
async def admin_tx() -> AsyncIterator[asyncpg.Connection]:
    """Transaction for genuinely global tables.

    Config bundles, prompt versions, model versions and prices are properties
    of the deployment rather than of a tenant, carry no RLS, and are read by
    every tenant when inspecting the provenance of its own answers.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn
