"""GitHub App webhook processing (Part 1 — production-ready).

Responsibilities (backend-only; never touches the agent or frontend):
  * Verify the ``X-Hub-Signature-256`` HMAC against GITHUB_APP_WEBHOOK_SECRET
    using a constant-time comparison over the RAW request body.
  * Replay protection: each ``X-GitHub-Delivery`` UUID is recorded once in
    ``github_webhook_deliveries``; a repeat delivery is skipped idempotently.
  * Dispatch validated events to per-event handlers, each isolated so a single
    handler failure cannot corrupt the others or crash the endpoint.
  * Every handled event: database update + cache invalidation (where relevant)
    + audit log.

Supported events (action in payload):
  installation:            created | deleted | suspend | unsuspend
  installation_repositories: added | removed
  repository:              deleted | renamed

Repository identity rule (approved architecture):
  * repo_id (immutable) is the canonical key used for webhook mapping.
  * repo_full_name (mutable) is updated on repository.renamed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Optional

from core.config import get_settings
from core.database import get_supabase_async
from services.github_audit import AuditEvent, record_github_event

logger = logging.getLogger(__name__)


class WebhookError(Exception):
    """Raised for signature / validation failures (mapped to HTTP by router)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Signature verification (constant-time HMAC over raw body)
# ---------------------------------------------------------------------------


def verify_signature(*, raw_body: bytes, signature_header: Optional[str]) -> None:
    """Verify ``X-Hub-Signature-256``. Raises WebhookError on any mismatch.

    Format of the header: ``sha256=<hexdigest>``.
    """
    settings = get_settings()
    secret = settings.GITHUB_APP_WEBHOOK_SECRET
    if not secret:
        raise WebhookError(
            status_code=503,
            code="WEBHOOK_NOT_CONFIGURED",
            message="GitHub App webhook secret is not configured.",
        )
    if not signature_header:
        raise WebhookError(
            status_code=401,
            code="MISSING_SIGNATURE",
            message="Missing X-Hub-Signature-256 header.",
        )
    if not signature_header.startswith("sha256="):
        raise WebhookError(
            status_code=401,
            code="BAD_SIGNATURE_FORMAT",
            message="Signature must be prefixed with 'sha256='.",
        )
    provided = signature_header.split("=", 1)[1].strip()
    computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, computed):
        raise WebhookError(
            status_code=401,
            code="INVALID_SIGNATURE",
            message="Webhook signature verification failed.",
        )


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------


async def _already_processed(*, delivery_id: str) -> bool:
    """Return True if this delivery UUID was already recorded (replay)."""
    supabase = await get_supabase_async()
    result = (
        await supabase.table("github_webhook_deliveries")
        .select("delivery_id")
        .eq("delivery_id", delivery_id)
        .maybe_single()
        .execute()
    )
    return bool(result and result.data)


async def _record_delivery(*, delivery_id: str, event_type: str, action: Optional[str]) -> None:
    """Persist the delivery UUID so a repeat is detected as a replay."""
    supabase = await get_supabase_async()
    await supabase.table("github_webhook_deliveries").insert(
        {"delivery_id": delivery_id, "event_type": event_type, "action": action}
    ).execute()


# ---------------------------------------------------------------------------
# Event handlers — each does DB update + cache invalidation + audit
# ---------------------------------------------------------------------------


async def _handle_installation(*, action: str, payload: dict[str, Any]) -> None:
    installation = payload.get("installation", {}) or {}
    installation_id = str(installation.get("id") or "")
    if not installation_id:
        logger.warning("[webhook] installation event without installation id")
        return
    supabase = await get_supabase_async()

    if action == "created":
        account = installation.get("account", {}) or {}
        record = {
            "id": installation_id,
            "github_account_id": str(account.get("id", "")),
            "github_account_login": account.get("login", ""),
            "github_account_type": account.get("type", "User"),
            "permissions": installation.get("permissions", {}) or {},
            "repositories_count": int(payload.get("repositories_count", 0) or 0),
            "status": "active",
        }
        # Upsert metadata; user_id may be absent here (installation via GitHub UI).
        await supabase.table("github_app_installations").upsert(
            record, on_conflict="id"
        ).execute()
        await record_github_event(
            AuditEvent(
                event_type="installation.created",
                installation_id=installation_id,
                status="ok",
                metadata={"account": account.get("login", "")},
            )
        )
        return

    if action == "deleted":
        # SOFT delete: mark status, invalidate token, cleanup app connections.
        await supabase.table("github_app_installations").update(
            {"status": "deleted"}
        ).eq("id", installation_id).execute()
        await _invalidate_token(installation_id)
        await _cleanup_app_connections(installation_id=installation_id)
        await record_github_event(
            AuditEvent(
                event_type="installation.deleted",
                installation_id=installation_id,
                status="ok",
                metadata={"soft_delete": True},
            )
        )
        return

    if action == "suspend":
        await supabase.table("github_app_installations").update(
            {"status": "suspended"}
        ).eq("id", installation_id).execute()
        await _invalidate_token(installation_id)
        await record_github_event(
            AuditEvent(
                event_type="installation.suspend",
                installation_id=installation_id,
                status="ok",
            )
        )
        return

    if action == "unsuspend":
        await supabase.table("github_app_installations").update(
            {"status": "active"}
        ).eq("id", installation_id).execute()
        # Invalidate: a suspended installation's token became unusable; after
        # unsuspend we must not serve the old one (Part 6 D2).
        await _invalidate_token(installation_id)
        await record_github_event(
            AuditEvent(
                event_type="installation.unsuspend",
                installation_id=installation_id,
                status="ok",
            )
        )
        return

    logger.info("[webhook] unhandled installation action=%s", action)


async def _handle_installation_repositories(*, action: str, payload: dict[str, Any]) -> None:
    installation = payload.get("installation", {}) or {}
    installation_id = str(installation.get("id") or "")
    if not installation_id:
        return
    supabase = await get_supabase_async()

    if action == "added":
        added = payload.get("repositories_added", []) or []
        # Keep the installation's repo count fresh for UX.
        await _bump_repo_count(installation_id=installation_id, delta=len(added))
        await record_github_event(
            AuditEvent(
                event_type="installation_repositories.added",
                installation_id=installation_id,
                status="ok",
                metadata={"count": len(added)},
            )
        )
        return

    if action == "removed":
        removed = payload.get("repositories_removed", []) or []
        await _bump_repo_count(installation_id=installation_id, delta=-len(removed))
        # Mark any workspace-linked connection whose repo was removed.
        for repo in removed:
            repo_id = repo.get("id")
            if repo_id is not None:
                await _mark_repo_removed(repo_id=int(repo_id))
        await record_github_event(
            AuditEvent(
                event_type="installation_repositories.removed",
                installation_id=installation_id,
                status="ok",
                metadata={"count": len(removed)},
            )
        )
        return

    logger.info("[webhook] unhandled installation_repositories action=%s", action)


async def _handle_repository(*, action: str, payload: dict[str, Any]) -> None:
    repo = payload.get("repository", {}) or {}
    repo_id = repo.get("id")
    if repo_id is None:
        logger.warning("[webhook] repository event without repo id")
        return
    repo_id = int(repo_id)

    if action == "deleted":
        await _mark_repo_removed(repo_id=repo_id)
        await record_github_event(
            AuditEvent(
                event_type="repository.deleted",
                repo_id=repo_id,
                repo_full_name=repo.get("full_name"),
                status="ok",
            )
        )
        return

    if action == "renamed":
        # Map by the immutable repo_id; only the mutable full_name changes.
        new_full_name = repo.get("full_name")
        if new_full_name:
            await _rename_repo(repo_id=repo_id, new_full_name=new_full_name)
        await record_github_event(
            AuditEvent(
                event_type="repository.renamed",
                repo_id=repo_id,
                repo_full_name=new_full_name,
                status="ok",
                metadata={"changes": payload.get("changes", {})},
            )
        )
        return

    logger.info("[webhook] unhandled repository action=%s", action)


# ---------------------------------------------------------------------------
# DB / cache helpers
# ---------------------------------------------------------------------------


async def _invalidate_token(installation_id: str) -> None:
    """Invalidate the cached installation token (Part 1 wired to webhook,
    Part 6 made async + per-installation-locked for consistency).
    """
    from services.github_app_service import invalidate_installation_token

    await invalidate_installation_token(installation_id)


async def _cleanup_app_connections(*, installation_id: str) -> None:
    """Mark app connections for a deleted installation as removed (no orphan tokens).

    We do NOT hard-delete the connection rows (workspaces may still FK-reference
    them); we mark their repo_status via repo_full_name-independent status. Since
    connections carry installation_id, we clear the ability to mint tokens by
    invalidating the cache (done by caller) and recording the cleanup.
    """
    supabase = await get_supabase_async()
    # Best-effort: annotate connections belonging to this installation.
    try:
        await supabase.table("github_connections").update(
            {"repo_full_name": None}
        ).eq("installation_id", installation_id).eq("auth_method", "app").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[webhook] app connection cleanup failed: %s", exc)


async def _bump_repo_count(*, installation_id: str, delta: int) -> None:
    supabase = await get_supabase_async()
    try:
        result = (
            await supabase.table("github_app_installations")
            .select("repositories_count")
            .eq("id", installation_id)
            .maybe_single()
            .execute()
        )
        current = int((result.data or {}).get("repositories_count", 0)) if result else 0
        new_count = max(0, current + delta)
        await supabase.table("github_app_installations").update(
            {"repositories_count": new_count}
        ).eq("id", installation_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[webhook] repo count update failed: %s", exc)


async def _mark_repo_removed(*, repo_id: int) -> None:
    """Flag connections whose canonical repo_id was removed/deleted upstream."""
    supabase = await get_supabase_async()
    try:
        await supabase.table("github_connections").update(
            {"repo_full_name": None}
        ).eq("repo_id", repo_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[webhook] mark repo removed failed: %s", exc)


async def _rename_repo(*, repo_id: int, new_full_name: str) -> None:
    """Update the mutable full_name for the connection identified by repo_id."""
    supabase = await get_supabase_async()
    try:
        await supabase.table("github_connections").update(
            {"repo_full_name": new_full_name}
        ).eq("repo_id", repo_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[webhook] repo rename failed: %s", exc)


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------


_DISPATCH = {
    "installation": _handle_installation,
    "installation_repositories": _handle_installation_repositories,
    "repository": _handle_repository,
}


async def process_webhook(
    *,
    raw_body: bytes,
    signature_header: Optional[str],
    event_type: Optional[str],
    delivery_id: Optional[str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Verify, de-duplicate, and dispatch a webhook. Returns a small status dict.

    Raises WebhookError for signature/validation failures (router maps to HTTP).
    Handler-level failures are isolated and reported, never propagated as 5xx
    unless the whole delivery cannot be recorded.

    Correlation (Part 7, D2): the webhook is NOT a special case. We apply the
    single rule - continue an existing correlation_id if present, otherwise
    mint one. Here we use the GitHub delivery_id as the correlation_id so the
    whole webhook to downstream (invalidate, lifecycle) chain shares it.
    """
    from services.github_audit import (
        AuditEvent,
        record_github_event,
        reset_correlation,
        set_correlation_id,
    )

    set_correlation_id(delivery_id)
    try:
        verify_signature(raw_body=raw_body, signature_header=signature_header)
        await record_github_event(
            AuditEvent(
                event_type="webhook.signature_verified",
                status="ok",
                metadata={"delivery_id": delivery_id},
            )
        )
    except WebhookError:
        await record_github_event(
            AuditEvent(
                event_type="webhook.signature_failed",
                status="failed",
                metadata={"delivery_id": delivery_id},
            )
        )
        raise

    if not event_type:
        raise WebhookError(
            status_code=400, code="MISSING_EVENT", message="Missing X-GitHub-Event header."
        )
    if not delivery_id:
        raise WebhookError(
            status_code=400, code="MISSING_DELIVERY", message="Missing X-GitHub-Delivery header."
        )

    installation_id = str((payload.get("installation", {}) or {}).get("id") or "") or None

    # Replay protection: skip if we've already recorded this delivery.
    if await _already_processed(delivery_id=delivery_id):
        logger.info("[webhook] replay skipped delivery=%s", delivery_id)
        await record_github_event(
            AuditEvent(
                event_type="webhook.replay_detected",
                status="replay",
                installation_id=installation_id,
                metadata={"delivery_id": delivery_id},
            )
        )
        return {"status": "replay", "delivery_id": delivery_id}

    action = payload.get("action")

    handler = _DISPATCH.get(event_type)
    if handler is None:
        # Unknown event type: record delivery + audit, return ignored (idempotent).
        await _record_delivery(delivery_id=delivery_id, event_type=event_type, action=action)
        await record_github_event(
            AuditEvent(
                event_type=f"webhook.ignored.{event_type}",
                installation_id=installation_id,
                status="ignored",
                metadata={"action": action},
            )
        )
        return {"status": "ignored", "event": event_type}

    # Error isolation: a handler failure is logged + audited but does not crash
    # the endpoint. We still record the delivery so GitHub does not hammer us.
    await record_github_event(
        AuditEvent(
            event_type="webhook.received",
            installation_id=installation_id,
            status="ok",
            metadata={"event": event_type, "action": action},
        )
    )
    try:
        await handler(action=action or "", payload=payload)
        status = "processed"
    except Exception as exc:  # noqa: BLE001
        logger.exception("[webhook] handler error event=%s action=%s: %s", event_type, action, exc)
        await record_github_event(
            AuditEvent(
                event_type="webhook.handler_error",
                installation_id=installation_id,
                status="failed",
                metadata={"event": event_type, "action": action, "error": str(exc)[:400]},
            )
        )
        status = "handler_error"

    await _record_delivery(delivery_id=delivery_id, event_type=event_type, action=action)
    reset_correlation()
    return {"status": status, "event": event_type, "action": action}
