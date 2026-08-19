"""GitHub API rate-limit layer (Part 3) — sits ON TOP OF the Part 2 wrapper.

ARCHITECTURE CONTRACT (approved):
    This module is an INDEPENDENT subsystem layered above ``github_request``
    (Part 2). It does NOT modify — and must never re-implement — the Part 2
    retry / backoff / timeout / transient-classification mechanism. It calls
    ``github_request`` and adds ONLY rate-limit handling on the returned
    response:

      * HTTP 429, or HTTP 403 with ``X-RateLimit-Remaining: 0`` (GitHub signals
        primary/secondary limits via either).
      * Wait computation precedence: Retry-After (if enabled) > X-RateLimit-Reset.
        If NEITHER header is present -> raise GitHubRateLimitError immediately.
      * Bounded, single wait-then-retry: wait at most ONCE and only when the
        computed wait is within GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS; otherwise
        raise GitHubRateLimitError immediately. NEVER loops.

    Business policy (whether to wait at all) is the caller's decision, passed
    via ``wait_on_rate_limit`` — e.g. OAuth code exchange passes False and gets
    an immediate error, never a wait.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from core.config import get_settings
from services.github_http import github_request
from services.github_audit import AuditEvent, record_github_event

logger = logging.getLogger(__name__)


async def _audit_rate(event_type: str, *, method: str, url: str, status: str, **extra: Any) -> None:
    """Fire-and-forget structured audit for a rate-limit event (Part 7)."""
    try:
        await record_github_event(
            AuditEvent(
                event_type=event_type,
                status=status,
                step_name="github_api_call",
                metadata={"method": method, "url": url, **extra},
            )
        )
    except Exception:  # noqa: BLE001 - observability must never break the caller
        pass


class GitHubRateLimitError(Exception):
    """Raised when a GitHub request is rate limited and cannot be satisfied.

    Carries ``retry_after`` (seconds) so the API layer can surface a precise
    HTTP 429 with a Retry-After header to the client.
    """

    def __init__(self, retry_after: int, message: str = "GitHub API rate limit exceeded.") -> None:
        super().__init__(message)
        self.retry_after = max(0, int(retry_after))
        self.message = message


def _is_rate_limited(resp: httpx.Response) -> bool:
    """True if the response indicates a GitHub rate limit.

    GitHub returns 429 for some limits and 403 with X-RateLimit-Remaining: 0
    for others (primary rate limit). A plain 403 (remaining > 0) is a genuine
    permission error and is NOT treated as a rate limit.
    """
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.strip() == "0":
            return True
    return False


def _compute_wait_seconds(resp: httpx.Response) -> Optional[float]:
    """Compute wait time from headers.

    Precedence (per approved architecture):
        1. Retry-After (when RESPECT_RETRY_AFTER) — seconds.
        2. X-RateLimit-Reset — absolute epoch seconds; wait = reset - now.
        3. Neither present -> None (caller raises immediately).
    """
    settings = get_settings()

    if settings.GITHUB_RATE_LIMIT_RESPECT_RETRY_AFTER:
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(int(retry_after.strip())))
            except (ValueError, TypeError):
                pass  # fall through to reset

    reset = resp.headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            reset_epoch = float(int(reset.strip()))
            return max(0.0, reset_epoch - time.time())
        except (ValueError, TypeError):
            pass

    return None


async def github_api_call(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    data: Any = None,
    json: Any = None,
    params: Any = None,
    retry: bool = False,
    timeout: Optional[float] = None,
    wait_on_rate_limit: bool = False,
) -> httpx.Response:
    """Perform a GitHub API call with rate-limit handling over the Part 2 wrapper.

    Args:
        method/url/headers/data/json/params/retry/timeout: forwarded UNCHANGED
            to ``github_request`` (Part 2). ``retry`` still controls the Part 2
            transient-retry mechanism and is independent from rate limiting.
        wait_on_rate_limit: BUSINESS DECISION from the caller. When False, a
            rate-limited response raises GitHubRateLimitError immediately (used
            by OAuth code exchange). When True, the call waits ONCE (bounded by
            GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS) and retries a single time.

    Returns:
        The httpx.Response when not rate limited (caller handles status as before).

    Raises:
        GitHubRateLimitError: when rate limited and (a) waiting is disabled, or
            (b) no wait header is present, or (c) the wait exceeds the cap, or
            (d) the single retry is still rate limited.
    """
    settings = get_settings()
    cap = settings.GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS

    # Part 2 layer (retry/backoff/timeout) — untouched.
    resp = await github_request(
        method, url, headers=headers, data=data, json=json, params=params,
        retry=retry, timeout=timeout,
    )

    if not _is_rate_limited(resp):
        return resp

    wait_seconds = _compute_wait_seconds(resp)

    # Caller opted out of waiting (e.g. OAuth exchange): fail fast.
    if not wait_on_rate_limit:
        logger.warning("[github_rate_limit] rate_limited | %s %s | wait_disabled", method, url)
        await _audit_rate("github.rate_limit", method=method, url=url, status="rate_limited",
                          wait_disabled=True, retry_after=int(wait_seconds or 0))
        raise GitHubRateLimitError(retry_after=int(wait_seconds or 0))

    # No usable header to compute a wait -> immediate error (no blind wait).
    if wait_seconds is None:
        logger.warning(
            "[github_rate_limit] rate_limited | %s %s | no Retry-After / X-RateLimit-Reset",
            method, url,
        )
        await _audit_rate("github.rate_limit", method=method, url=url, status="rate_limited",
                          retry_after=0, reason="no_wait_header")
        raise GitHubRateLimitError(retry_after=0)

    # Wait exceeds the configured cap -> do not wait, fail fast with 429.
    if wait_seconds > cap:
        logger.warning(
            "[github_rate_limit] rate_limit_exceeded_cap | %s %s | wait=%.1fs cap=%.1fs",
            method, url, wait_seconds, cap,
        )
        await _audit_rate("github.rate_limit", method=method, url=url, status="exceeded_cap",
                          wait_seconds=round(wait_seconds, 1), cap=round(cap, 1),
                          retry_after=int(wait_seconds))
        raise GitHubRateLimitError(retry_after=int(wait_seconds))

    # Bounded, single wait-then-retry (NEVER loops).
    logger.warning(
        "[github_rate_limit] rate_limit_wait | %s %s | waiting=%.1fs (once)",
        method, url, wait_seconds,
    )
    await _audit_rate("github.rate_limit", method=method, url=url, status="waiting",
                      wait_seconds=round(wait_seconds, 1), cap=round(cap, 1), once=True)
    await asyncio.sleep(wait_seconds)

    resp2 = await github_request(
        method, url, headers=headers, data=data, json=json, params=params,
        retry=retry, timeout=timeout,
    )

    if _is_rate_limited(resp2):
        # Still limited after the single retry -> fail fast, do NOT loop.
        retry_after2 = _compute_wait_seconds(resp2)
        logger.warning(
            "[github_rate_limit] rate_limited_after_retry | %s %s | giving up (no loop)",
            method, url,
        )
        await _audit_rate("github.rate_limit", method=method, url=url, status="rate_limited_after_retry",
                          retry_after=int(retry_after2 or 0))
        raise GitHubRateLimitError(retry_after=int(retry_after2 or 0))

    return resp2
