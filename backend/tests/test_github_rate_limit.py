"""Tests for the GitHub rate-limit layer (Part 3).

Architecture guarantees under test:
  * Rate limiting is a layer ON TOP OF github_request (Part 2) — Part 2 is
    patched, never modified, and is shown to be invoked unchanged.
  * 429 and 403+X-RateLimit-Remaining:0 are treated as rate limits.
  * A plain 403 (remaining > 0) is NOT a rate limit.
  * Wait precedence: Retry-After > X-RateLimit-Reset > (neither -> immediate error).
  * Bounded, single wait-then-retry; never loops.
  * wait_on_rate_limit=False fails fast (OAuth exchange semantics).
  * Wait exceeding the cap fails fast.
  * GitHubRateLimitError carries retry_after for the API 429 mapping.

asyncio.sleep is patched so tests never really wait.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from services import github_rate_limit
from services.github_rate_limit import GitHubRateLimitError, github_api_call


class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _run(coro):
    return asyncio.run(coro)


class _Sequence:
    """Scripted github_request replacement recording call count."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.seen_kwargs = []

    async def __call__(self, method, url, **kwargs):
        self.seen_kwargs.append(kwargs)
        item = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return item


def _patch(seq):
    return patch.object(github_rate_limit, "github_request", new=seq)


def _no_sleep():
    return patch("services.github_rate_limit.asyncio.sleep", new=AsyncMock())


# ---------------------------------------------------------------------------
# Not rate limited: pass-through
# ---------------------------------------------------------------------------


def test_non_rate_limited_passthrough():
    seq = _Sequence([_Resp(200)])
    with _patch(seq), _no_sleep():
        resp = _run(github_api_call("GET", "http://x", retry=True, wait_on_rate_limit=True))
    assert resp.status_code == 200
    assert seq.calls == 1
    # Part 2 forwarding contract: retry flag passed through unchanged.
    assert seq.seen_kwargs[0]["retry"] is True


def test_plain_403_is_not_rate_limit():
    seq = _Sequence([_Resp(403, {"X-RateLimit-Remaining": "42"})])
    with _patch(seq), _no_sleep():
        resp = _run(github_api_call("GET", "http://x", retry=True, wait_on_rate_limit=True))
    # Genuine permission error -> returned as-is, NOT treated as rate limit.
    assert resp.status_code == 403
    assert seq.calls == 1


# ---------------------------------------------------------------------------
# wait_on_rate_limit=False (OAuth exchange semantics): fail fast
# ---------------------------------------------------------------------------


def test_wait_disabled_fails_fast_on_429():
    seq = _Sequence([_Resp(429, {"Retry-After": "5"})])
    with _patch(seq), _no_sleep():
        with pytest.raises(GitHubRateLimitError) as exc:
            _run(github_api_call("POST", "http://x", retry=False, wait_on_rate_limit=False))
    assert exc.value.retry_after == 5
    assert seq.calls == 1  # no wait, no retry


# ---------------------------------------------------------------------------
# Retry-After precedence + bounded wait-then-retry
# ---------------------------------------------------------------------------


def test_retry_after_within_cap_waits_once_then_succeeds():
    seq = _Sequence([_Resp(429, {"Retry-After": "3"}), _Resp(200)])
    sleep = AsyncMock()
    with _patch(seq), patch("services.github_rate_limit.asyncio.sleep", new=sleep):
        resp = _run(github_api_call("GET", "http://x", retry=True, wait_on_rate_limit=True))
    assert resp.status_code == 200
    assert seq.calls == 2  # first + single retry
    sleep.assert_awaited_once()
    assert abs(sleep.await_args.args[0] - 3.0) < 1e-6


def test_403_remaining_zero_is_rate_limit_and_waits():
    seq = _Sequence([_Resp(403, {"X-RateLimit-Remaining": "0", "Retry-After": "2"}), _Resp(200)])
    with _patch(seq), _no_sleep():
        resp = _run(github_api_call("GET", "http://x", retry=True, wait_on_rate_limit=True))
    assert resp.status_code == 200
    assert seq.calls == 2


# ---------------------------------------------------------------------------
# X-RateLimit-Reset used when no Retry-After
# ---------------------------------------------------------------------------


def test_reset_header_used_when_no_retry_after():
    reset = int(time.time()) + 4
    seq = _Sequence([_Resp(429, {"X-RateLimit-Reset": str(reset)}), _Resp(200)])
    sleep = AsyncMock()
    with _patch(seq), patch("services.github_rate_limit.asyncio.sleep", new=sleep):
        resp = _run(github_api_call("GET", "http://x", retry=True, wait_on_rate_limit=True))
    assert resp.status_code == 200
    assert seq.calls == 2
    # waited roughly reset-now (~4s), within a tolerance band.
    waited = sleep.await_args.args[0]
    assert 2.0 <= waited <= 5.0


# ---------------------------------------------------------------------------
# Neither header present -> immediate error
# ---------------------------------------------------------------------------


def test_no_headers_fails_fast():
    seq = _Sequence([_Resp(429, {})])
    with _patch(seq), _no_sleep():
        with pytest.raises(GitHubRateLimitError):
            _run(github_api_call("GET", "http://x", retry=True, wait_on_rate_limit=True))
    assert seq.calls == 1  # no blind wait


# ---------------------------------------------------------------------------
# Wait exceeds cap -> immediate error, no wait
# ---------------------------------------------------------------------------


def test_wait_exceeding_cap_fails_fast():
    seq = _Sequence([_Resp(429, {"Retry-After": "99999"})])
    sleep = AsyncMock()
    with _patch(seq), patch("services.github_rate_limit.asyncio.sleep", new=sleep):
        with pytest.raises(GitHubRateLimitError) as exc:
            _run(github_api_call("GET", "http://x", retry=True, wait_on_rate_limit=True))
    assert exc.value.retry_after == 99999
    assert seq.calls == 1
    sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# Still limited after single retry -> fail fast, NO loop
# ---------------------------------------------------------------------------


def test_still_limited_after_retry_no_loop():
    seq = _Sequence([_Resp(429, {"Retry-After": "1"}), _Resp(429, {"Retry-After": "1"})])
    with _patch(seq), _no_sleep():
        with pytest.raises(GitHubRateLimitError):
            _run(github_api_call("GET", "http://x", retry=True, wait_on_rate_limit=True))
    assert seq.calls == 2  # first + exactly one retry, then give up (no loop)


# ---------------------------------------------------------------------------
# Part 2 independence: retry flag forwarded, github_request untouched
# ---------------------------------------------------------------------------


def test_part2_retry_flag_forwarded():
    seq = _Sequence([_Resp(200)])
    with _patch(seq), _no_sleep():
        _run(github_api_call("POST", "http://x", retry=False, wait_on_rate_limit=False, timeout=12.0))
    kw = seq.seen_kwargs[0]
    assert kw["retry"] is False
    assert kw["timeout"] == 12.0
