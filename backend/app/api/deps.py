"""Request-scoped dependencies.

The identity seam lives here. Today there is one user, resolved from the seeded
default tenant. When real authentication arrives, ``current_principal`` is the
only function that changes -- everything downstream already takes
``(tenant_id, user_id)`` and every query already runs under RLS, so no handler
and no SQL function needs touching.

That is the practical payoff of building multi-tenancy in from the start: the
migration is one function, not a schema change plus an audit of every query.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Request

from app.core.settings import Settings, get_settings


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
    email: str
    tenant_slug: str


async def current_principal(
    request: Request, settings: Settings = Depends(get_settings)
) -> Principal:
    """Resolve who is asking.

    Single-user today. The lookup is cached on app state because it is the same
    row on every request and it must not become a per-request round trip.
    """
    cached = getattr(request.app.state, "principal", None)
    if cached is not None:
        return cached

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # This is the one lookup that legitimately precedes the RLS context,
        # because it is what establishes it. A direct SELECT here cannot work:
        # the policy on `tenants` calls app_current_tenant(), which raises
        # while the GUC is unset. fn_bootstrap_principal is a narrow
        # SECURITY DEFINER function that exists solely to break that cycle.
        row = await conn.fetchrow(
            "SELECT * FROM fn_bootstrap_principal($1)",
            settings.default_tenant_slug,
        )
    if row is None:
        raise RuntimeError(
            f"no seeded tenant {settings.default_tenant_slug!r}; run `make seed`"
        )

    principal = Principal(
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        email=row["email"],
        tenant_slug=row["tenant_slug"],
    )
    request.app.state.principal = principal
    return principal


def get_bundle(request: Request):
    """The config bundle pinned for this process.

    Pinned at startup rather than re-read per request, so a YAML edit mid-flight
    cannot leave one run half-attributed to two different config versions.
    """
    return request.app.state.bundle


def get_resolver(request: Request):
    return request.app.state.resolver
