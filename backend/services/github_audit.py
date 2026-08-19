"""Structured audit logging for GitHub App events (Part 1, EXTENDED in Part 7).

Scope (Part 7):
    Part 7 does NOT create a parallel audit system. It extends the SINGLE
    existing ``github_audit_log`` table and the SINGLE ``record_github_event``
    entry point with an ``AuditEvent`` model so that every observability signal
    (webhook, lifecycle/saga, retry, rate-limit, cache) flows through ONE
    structured format.

    Business logic in Part 1-6 is NOT modified — only audit calls are added.

Correlation (Part 7, D2):
    A single ``correlation_id`` is threaded through every entry point via a
    contextvar. Entry points follow one rule: if a correlation_id is already
    present, continue it; otherwise mint a new one. This applies uniformly to
    HTTP requests, webhooks, future background jobs / queue workers / CLI /
    schedulers — the webhook is NOT a special case. ``request_id`` is the
    per-HTTP-request id (also stored on the contextvar); it may be unset for
    non-HTTP entry points.

Security contract:
    Secrets are NEVER written to the audit log. Every metadata value is run
    through a redactor that (a) drops known-sensitive KEY names and (b) masks
    known-sensitive VALUE patterns (Authorization headers, Bearer tokens,
    GitHub ``ghp_/gho_/ghu_/ghr_/github_pat_`` tokens, PEM blocks, private
    keys). The audit write is best-effort: a logging failure must never break
    the caller's primary path.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from core.database import get_supabase_async

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Correlation (D2) — one rule, all entry points
# ---------------------------------------------------------------------------


_correlation_id: ContextVar[Optional[str]] = ContextVar("github_audit_correlation_id", default=None)
_request_id: ContextVar[Optional[str]] = ContextVar("github_audit_request_id", default=None)


def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()


def get_request_id() -> Optional[str]:
    return _request_id.get()


def set_correlation_id(cid: Optional[str]) -> None:
    """Continue an existing correlation, or mint a new one if none is present.

    Called by EVERY entry point with the same rule: pass the incoming id if you
    have one, else pass None to auto-generate. Idempotent when already set.
    """
    existing = _correlation_id.get()
    if existing:
        return
    _correlation_id.set(cid or f"corr_{uuid.uuid4().hex}")


def set_request_id(rid: Optional[str]) -> None:
    _request_id.set(rid or f"req_{uuid.uuid4().hex}")


def reset_correlation() -> None:
    """Clear correlation context (called at the end of an entry-point scope)."""
    _correlation_id.set(None)
    _request_id.set(None)


# ---------------------------------------------------------------------------
# Redaction (key + value based)
# ---------------------------------------------------------------------------


_SENSITIVE_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "signature",
    "authorization",
    "pem",
    "private_key",
    "client_secret",
    "access_key",
    "api_key",
    "cookie",
    "session",
    "jwt",
    "refresh_token",
    "oauth",
)

_REDACTED = "[REDACTED]"

# Sensitive VALUE patterns (case-insensitive).
_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    if any(marker in k for marker in _SENSITIVE_KEY_MARKERS):
        return True
    if k == "key" or k.endswith("_key"):
        return True
    return False


def _redact_value(value: Any) -> Any:
    """Mask sensitive VALUE patterns in strings (best-effort)."""
    if isinstance(value, str):
        for pat in _VALUE_PATTERNS:
            value = pat.sub(_REDACTED, value)
    return value


def _redact(value: Any) -> Any:
    """Recursively redact sensitive keys and sensitive value patterns."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_key(str(k)):
                out[str(k)] = _REDACTED
            else:
                out[str(k)] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _redact_value(value)
    return value


# ---------------------------------------------------------------------------
# AuditEvent model (D1) — stable, extensible API
# ---------------------------------------------------------------------------


@dataclass
class AuditEvent:
    """Single structured audit event.

    Adding fields here never breaks existing callers: ``record_github_event``
    accepts an ``AuditEvent`` and reads attributes defensively.
    """

    event_type: str
    installation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    github_connection_id: Optional[str] = None
    server_id: Optional[str] = None
    user_id: Optional[str] = None
    request_id: Optional[str] = None
    correlation_id: Optional[str] = None
    step_name: Optional[str] = None
    duration_ms: Optional[int] = None
    status: Optional[str] = None
    repo_id: Optional[int] = None
    repo_full_name: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_context(self) -> "AuditEvent":
        """Fill correlation/request ids from the ambient context if unset."""
        if self.correlation_id is None:
            self.correlation_id = get_correlation_id()
        if self.request_id is None:
            self.request_id = get_request_id()
        return self


# ---------------------------------------------------------------------------
# Single entry point
# ---------------------------------------------------------------------------


async def record_github_event(event: AuditEvent) -> None:
    """Append a structured GitHub App event to ``github_audit_log`` (best-effort).

    Never raises: an audit write failure is logged as a warning and swallowed so
    the caller's primary path is never disrupted. Secrets in ``metadata`` are
    redacted before persistence.
    """
    event = event.with_context()
    safe_metadata = _redact(event.metadata or {})
    record = {
        "event_type": event.event_type,
        "installation_id": event.installation_id,
        "user_id": event.user_id,
        "repo_id": event.repo_id,
        "repo_full_name": event.repo_full_name,
        # Part 7 extended columns (additive; safe when migration not yet run).
        "workspace_id": event.workspace_id,
        "github_connection_id": event.github_connection_id,
        "server_id": event.server_id,
        "request_id": event.request_id,
        "correlation_id": event.correlation_id,
        "step_name": event.step_name,
        "duration_ms": event.duration_ms,
        "status": event.status,
        "metadata": safe_metadata,
    }
    # Structured application log (no secrets — metadata already redacted).
    logger.info(
        "[github_audit] event=%s correlation=%s status=%s",
        event.event_type,
        event.correlation_id,
        event.status,
    )
    try:
        supabase = await get_supabase_async()
        await supabase.table("github_audit_log").insert(record).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[github_audit] audit write failed for event=%s: %s", event.event_type, exc)


# ---------------------------------------------------------------------------
# Backwards-compatible shim for legacy callers (Part 1 / Part 4)
# ---------------------------------------------------------------------------


async def record_github_event_legacy(
    *,
    event_type: str,
    installation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    repo_id: Optional[int] = None,
    repo_full_name: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Compatibility wrapper for pre-Part-7 callers (keyword args)."""
    await record_github_event(
        AuditEvent(
            event_type=event_type,
            installation_id=installation_id,
            user_id=user_id,
            repo_id=repo_id,
            repo_full_name=repo_full_name,
            metadata=metadata or {},
        )
    )
