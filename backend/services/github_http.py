"""Centralised GitHub API HTTP wrapper with production retry/backoff (Part 2).

DESIGN CONTRACT (approved architecture):
    This module is a PURE MECHANISM. It knows how to retry, back off, jitter,
    time out, and classify *transient* transport failures. It knows NOTHING
    about business semantics — which endpoints are safe to retry is ALWAYS the
    caller's decision, passed explicitly via ``retry=True|False``.

    ``github_request`` never inspects the URL/path to guess a retry policy.
    When a new GitHub API call is added elsewhere, only the call site decides
    its retry policy; this module does not change.

RESPONSIBILITIES (mechanism only):
    * retry loop + attempt accounting
    * exponential backoff with jitter
    * per-attempt timeout
    * transient-error classification (network reset/timeout, HTTP 502/503/504)
    * retry limit

NOT handled here:
    * business decision of whether to retry (caller passes retry=)
    * rate limiting (429 / Retry-After / X-RateLimit) — that is Part 3
    * mapping to HTTPException — callers keep their existing status mapping
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Optional

import httpx

from core.config import get_settings
from services.github_audit import AuditEvent, record_github_event

logger = logging.getLogger(__name__)


# Transport-level exceptions that indicate a transient failure worth retrying.
_TRANSIENT_EXCEPTIONS = (
    httpx.TimeoutException,     # connect/read/write/pool timeouts
    httpx.ConnectError,        # connection refused / DNS
    httpx.ReadError,           # connection reset while reading
    httpx.WriteError,          # connection reset while writing
    httpx.RemoteProtocolError,  # server closed connection unexpectedly
)

# HTTP status codes considered transient (server-side, safe to retry).
# NOTE: 429 is intentionally NOT here — rate limiting is Part 3.
_TRANSIENT_STATUS = frozenset({502, 503, 504})


def _get_client() -> httpx.AsyncClient:
    """Return the shared pooled client, or a short-lived fallback client."""
    try:
        from services.http_client import get_http_client

        return get_http_client()
    except Exception:
        # Fallback (e.g. pool not initialised in a test/script context).
        return httpx.AsyncClient(timeout=get_settings().GITHUB_API_TIMEOUT_SECONDS)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter for a 0-based ``attempt`` index."""
    settings = get_settings()
    base = settings.GITHUB_API_BACKOFF_BASE_SECONDS
    cap = settings.GITHUB_API_BACKOFF_CAP_SECONDS
    jitter = settings.GITHUB_API_BACKOFF_JITTER_SECONDS
    delay = min(base * (2 ** attempt), cap)
    return delay + random.uniform(0.0, jitter)


def _is_transient_response(resp: httpx.Response) -> bool:
    return resp.status_code in _TRANSIENT_STATUS


async def _audit_retry(*, method: str, url: str, attempt: int, total: int,
                       status: str, reason: str, delay: float | None = None,
                       status_code: int | None = None) -> None:
    """Fire-and-forget structured audit for a retry attempt (Part 7)."""
    try:
        await record_github_event(
            AuditEvent(
                event_type="github.retry",
                status=status,
                step_name="github_request",
                metadata={
                    "method": method,
                    "url": url,
                    "attempt": attempt,
                    "total_attempts": total,
                    "reason": reason,
                    "delay_seconds": round(delay, 3) if delay is not None else None,
                    "status_code": status_code,
                },
            )
        )
    except Exception:  # noqa: BLE001
        pass


async def github_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    data: Any = None,
    json: Any = None,
    params: Any = None,
    retry: bool = False,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> httpx.Response:
    """Perform a GitHub API request with optional transient-failure retries.

    Args:
        method:  HTTP verb ("GET", "POST", ...).
        url:     Fully-qualified request URL.
        headers/data/json/params: passed through to httpx.
        retry:   BUSINESS DECISION supplied by the caller. When False, the
                 request is attempted exactly once (no retry loop) — used for
                 non-idempotent operations (e.g. OAuth code exchange). When
                 True, transient failures are retried with backoff.
        timeout: per-attempt timeout (defaults to GITHUB_API_TIMEOUT_SECONDS).
        max_retries: override the configured retry count (mechanism tuning).

    Returns:
        The httpx.Response for the final attempt. Non-transient HTTP errors
        (e.g. 401/403/404/422) are returned as-is for the caller to handle;
        this wrapper does NOT raise on HTTP status.

    Raises:
        The last transient transport exception if all attempts are exhausted.
    """
    settings = get_settings()
    per_attempt_timeout = timeout if timeout is not None else settings.GITHUB_API_TIMEOUT_SECONDS

    if not retry:
        # Single attempt: caller opted out of retries for semantic reasons.
        client = _get_client()
        return await client.request(
            method, url, headers=headers, data=data, json=json, params=params,
            timeout=per_attempt_timeout,
        )

    attempts = (max_retries if max_retries is not None else settings.GITHUB_API_MAX_RETRIES)
    total_tries = max(1, attempts + 1)  # attempts=retries beyond the first try
    last_exc: Optional[Exception] = None

    for attempt in range(total_tries):
        client = _get_client()
        try:
            resp = await client.request(
                method, url, headers=headers, data=data, json=json, params=params,
                timeout=per_attempt_timeout,
            )
        except _TRANSIENT_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < total_tries - 1:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "[github_http] retry started | %s %s | attempt=%d/%d | transient=%s | sleep=%.2fs",
                    method, url, attempt + 1, total_tries, type(exc).__name__, delay,
                )
                await _audit_retry(method=method, url=url, attempt=attempt + 1,
                                   total=total_tries, status="retry_scheduled",
                                   reason=f"transport:{type(exc).__name__}", delay=delay)
                await asyncio.sleep(delay)
                logger.info(
                    "[github_http] retry finished | %s %s | attempt=%d",
                    method, url, attempt + 1,
                )
                continue
            logger.error(
                "[github_http] retries exhausted | %s %s | attempts=%d | last=%s",
                method, url, total_tries, type(exc).__name__,
            )
            await _audit_retry(method=method, url=url, attempt=total_tries,
                               total=total_tries, status="exhausted",
                               reason=f"transport:{type(exc).__name__}")
            raise
        # We have a response — retry only on transient status codes.
        if _is_transient_response(resp) and attempt < total_tries - 1:
            delay = _backoff_delay(attempt)
            logger.warning(
                "[github_http] retry started | %s %s | attempt=%d/%d | status=%d | sleep=%.2fs",
                method, url, attempt + 1, total_tries, resp.status_code, delay,
            )
            await _audit_retry(method=method, url=url, attempt=attempt + 1,
                               total=total_tries, status="retry_scheduled",
                               reason="status", status_code=resp.status_code, delay=delay)
            await asyncio.sleep(delay)
            logger.info(
                "[github_http] retry finished | %s %s | attempt=%d",
                method, url, attempt + 1,
            )
            continue
        return resp

    # Unreachable in practice, but keeps type-checkers happy.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("github_request: exhausted retries without a response")
