"""Centralized authorization enforcement (Production Hardening D5).

Background
----------
Authorization in ThinkSync is application-layer only: the backend connects to
Supabase with the **service-role** key, which bypasses row-level security, so
tenant isolation depends entirely on the backend always scoping queries by the
calling user's id. Row-level security (RLS) was dropped because the service-role
key circumvents it; reintroducing RLS would require per-request user JWTs for the
database connection (a full authentication redesign) and is therefore out of
scope for this hardening sprint. The strongest production-safe guarantee we can
provide without that redesign is a *single, centralized* enforcement chokepoint
that makes accidental omission of the tenant filter impossible for code that uses
it, plus an explicit ownership assertion for the fetch-by-id-then-verify pattern
that already exists in ``services.permission_service``.

This module provides:
  * ``TENANT_TABLES``   – registry of tables that carry a ``user_id`` column and
                         therefore MUST be tenant-scoped.
  * ``assert_owns``     – fail-closed ownership check used by the
                         fetch-by-id-then-verify pattern. Raises ``403`` when the
                         resource does not belong to the caller.
  * ``tenant_query``    – async builder factory that ALWAYS prepends
                         ``.eq("user_id", user_id)`` so a query built through it
                         can never accidentally omit the tenant filter.

Nothing here changes the JWT format, Google OAuth, or the frontend.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from core.database import get_supabase_async

# Tables that own tenant data and carry a ``user_id`` column. Any read/write on
# these from a user-facing request MUST be scoped by ``user_id`` (or verified via
# ``assert_owns`` against an FK that is itself user-scoped).
TENANT_TABLES = frozenset(
    {
        "servers",
        "workspaces",
        "chats",
        "jobs",
        "conversations",
        "approval_requests",
        "workspace_files",
        "project_specifications",
        "idempotency_store",
        "resume_outcomes",
        "conversation_audit",
        "approval_audit",
        "workspace_deployments",
    }
)


class TenantIsolationError(HTTPException):
    """Raised when a tenant-owned resource is accessed across user boundaries."""


def assert_owns(
    resource_user_id: Any,
    current_user_id: str,
    resource_type: str = "resource",
) -> None:
    """Fail-closed ownership check for the fetch-by-id-then-verify pattern.

    Compares the ``user_id`` stored on a resource against the authenticated
    caller's id. Raises ``403`` on any mismatch or missing ownership data so the
    default posture is *deny*.

    This centralizes the manual ``if data.get("user_id") != user_id: return False``
    checks previously duplicated in ``services.permission_service``.
    """
    if resource_user_id is None:
        raise TenantIsolationError(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{resource_type} ownership could not be verified.",
        )
    if str(resource_user_id) != str(current_user_id):
        raise TenantIsolationError(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{resource_type} does not belong to the calling user.",
        )


async def tenant_query(table: str, user_id: str):
    """Return an async Supabase query builder pre-scoped to ``user_id``.

    Every chain started from this builder already carries
    ``.eq("user_id", user_id)``, eliminating accidental omission of the tenant
    filter for user-facing reads/writes. Use this instead of
    ``get_supabase_async().table(table)`` whenever the table is in
    ``TENANT_TABLES``.

    Example
    -------
    rows = await tenant_query("workspaces", user_id).select("*").execute()
    """
    if table not in TENANT_TABLES:
        # Defensive: never silently scope a non-tenant table.
        raise ValueError(f"{table!r} is not a registered tenant table")
    client = await get_supabase_async()
    return client.table(table).eq("user_id", user_id)
