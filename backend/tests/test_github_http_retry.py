"""Tests for the GitHub API retry/backoff wrapper (Part 2).

Verifies the wrapper is a pure mechanism:
  * retry=True retries transient transport errors (timeout/connect reset)
  * retry=True retries transient HTTP status (502/503/504)
  * retry=True does NOT retry permanent HTTP status (401/403/404/422) or 429
  * retry=False attempts exactly once (no retry loop)
  * retries are bounded by GITHUB_API_MAX_RETRIES and the last error surfaces
  * backoff delay is exponential and jitter-bounded

asyncio.sleep is patched to avoid real delays.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import github_http
from services.github_http import github_request, _backoff_delay


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """Records requests; yields a scripted sequence of responses/exceptions."""

    def __init__(self, sequence):
        self._seq = list(sequence)
        self.calls = 0

    async def request(self, method, url, **kwargs):
        self.calls += 1
        item = self._seq[min(self.calls - 1, len(self._seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _run(coro):
    return asyncio.run(coro)


def _with_client(client):
    # Patch the pooled client getter + make sleep instant.
    return patch.object(github_http, "_get_client", return_value=client)


def _no_sleep():
    return patch("services.github_http.asyncio.sleep", new=AsyncMock())


# ---------------------------------------------------------------------------
# Transient transport exceptions
# ---------------------------------------------------------------------------


def test_retry_true_recovers_from_timeout():
    client = _FakeClient([httpx.ReadTimeout("t"), httpx.ReadTimeout("t"), _Resp(200)])
    with _with_client(client), _no_sleep():
        resp = _run(github_request("GET", "http://x", retry=True, max_retries=3))
    assert resp.status_code == 200
    assert client.calls == 3


def test_retry_true_recovers_from_connection_reset():
    client = _FakeClient([httpx.ReadError("reset"), _Resp(200)])
    with _with_client(client), _no_sleep():
        resp = _run(github_request("GET", "http://x", retry=True, max_retries=3))
    assert resp.status_code == 200
    assert client.calls == 2


def test_retry_true_exhausts_and_raises_last_exception():
    client = _FakeClient([httpx.ConnectError("down")])
    with _with_client(client), _no_sleep():
        with pytest.raises(httpx.ConnectError):
            _run(github_request("GET", "http://x", retry=True, max_retries=2))
    # initial try + 2 retries = 3 attempts
    assert client.calls == 3


# ---------------------------------------------------------------------------
# Transient HTTP status codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [502, 503, 504])
def test_retry_true_retries_transient_status(code):
    client = _FakeClient([_Resp(code), _Resp(200)])
    with _with_client(client), _no_sleep():
        resp = _run(github_request("GET", "http://x", retry=True, max_retries=3))
    assert resp.status_code == 200
    assert client.calls == 2


def test_retry_true_returns_last_transient_status_after_exhaustion():
    client = _FakeClient([_Resp(503)])
    with _with_client(client), _no_sleep():
        resp = _run(github_request("GET", "http://x", retry=True, max_retries=2))
    assert resp.status_code == 503
    assert client.calls == 3  # initial + 2 retries


# ---------------------------------------------------------------------------
# Permanent status codes are NOT retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403, 404, 422, 429])
def test_retry_true_does_not_retry_permanent_status(code):
    client = _FakeClient([_Resp(code), _Resp(200)])
    with _with_client(client), _no_sleep():
        resp = _run(github_request("GET", "http://x", retry=True, max_retries=3))
    assert resp.status_code == code
    assert client.calls == 1  # returned immediately, no retry


# ---------------------------------------------------------------------------
# retry=False: single attempt
# ---------------------------------------------------------------------------


def test_retry_false_single_attempt_on_transient():
    client = _FakeClient([httpx.ReadTimeout("t"), _Resp(200)])
    with _with_client(client), _no_sleep():
        with pytest.raises(httpx.ReadTimeout):
            _run(github_request("POST", "http://x", retry=False))
    assert client.calls == 1  # no retry loop entered


def test_retry_false_returns_response_without_retry_on_5xx():
    client = _FakeClient([_Resp(503), _Resp(200)])
    with _with_client(client), _no_sleep():
        resp = _run(github_request("POST", "http://x", retry=False))
    assert resp.status_code == 503
    assert client.calls == 1


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------


def test_backoff_is_exponential_and_jitter_bounded():
    # With defaults base=0.5, cap=8.0, jitter=0.3:
    #   attempt 0 -> [0.5, 0.8), attempt 1 -> [1.0, 1.3), attempt 2 -> [2.0, 2.3)
    d0 = _backoff_delay(0)
    d1 = _backoff_delay(1)
    d2 = _backoff_delay(2)
    assert 0.5 <= d0 < 0.8 + 1e-9
    assert 1.0 <= d1 < 1.3 + 1e-9
    assert 2.0 <= d2 < 2.3 + 1e-9


def test_backoff_respects_cap():
    # Large attempt index must be capped at cap (+jitter).
    d = _backoff_delay(20)
    assert d < 8.0 + 0.3 + 1e-9
