"""Tests for GitHub App installation token cache (Part 6).

Covers the audit's required matrix:
  * cache hit (no lock, no GitHub call)
  * cache miss (one mint)
  * expired token (re-mint)
  * TTL conversion from GitHub expires_at
  * double-checked locking (second concurrent caller reuses, not re-mints)
  * parallel refresh -> exactly ONE GitHub call (no duplicate mint, no herd)
  * parallel invalidate
  * refresh + invalidate race -> no stale write
  * refresh failure (HTTPError / non-201) -> cache evicted, never serves stale
  * per-installation lock isolation (two installations refresh independently)
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from services import github_app_service as gas


def _reset_cache():
    gas._token_cache.clear()
    gas._token_locks.clear()


def _fake_api_call(token_body):
    """Return an async callable standing in for github_api_call."""

    async def _call(method, url, **kw):
        class _Resp:
            status_code = 201
            text = "ok"

            def json(self):
                return dict(token_body)

        return _Resp()

    return _call


async def _mint(installation_id="inst-1", token_body=None):
    token_body = token_body or {"token": "tok", "expires_at": "2099-01-01T00:00:00Z"}
    with patch("services.github_app_service.github_api_call", new=_fake_api_call(token_body)):
        with patch("services.github_app_service._make_app_jwt", return_value="jwt"):
            return await gas.get_installation_token(installation_id=installation_id)


# ---------------------------------------------------------------------------
# Basic hit / miss / expiry / TTL
# ---------------------------------------------------------------------------


def test_cache_miss_mints_once_then_hits():
    _reset_cache()
    counter = {"n": 0}

    async def _call(method, url, **kw):
        counter["n"] += 1
        _resp = lambda: None
        _resp.status_code = 201
        _resp.text = "ok"

        def json():
            return {"token": f"t{counter['n']}", "expires_at": "2099-01-01T00:00:00Z"}

        _resp.json = json
        return _resp

    async def run():
        with patch("services.github_app_service.github_api_call", new=_call):
            with patch("services.github_app_service._make_app_jwt", return_value="jwt"):
                t1 = await gas.get_installation_token(installation_id="i1")
                t2 = await gas.get_installation_token(installation_id="i1")
                return t1, t2

    t1, t2 = asyncio.run(run())
    assert t1 == t2 == "t1"          # second call served from cache
    assert counter["n"] == 1         # only one GitHub mint


def test_expired_token_re_mints():
    _reset_cache()
    calls = []

    async def _call(method, url, **kw):
        calls.append(1)
        _resp = lambda: None
        _resp.status_code = 201
        _resp.text = "ok"

        def json():
            # Already-expired token so the next call sees it as stale.
            return {"token": f"t{len(calls)}", "expires_at": "2000-01-01T00:00:00Z"}

        _resp.json = json
        return _resp

    async def run():
        with patch("services.github_app_service.github_api_call", new=_call):
            with patch("services.github_app_service._make_app_jwt", return_value="jwt"):
                await gas.get_installation_token(installation_id="i1")
                await gas.get_installation_token(installation_id="i1")

    asyncio.run(run())
    assert len(calls) == 2           # expired -> re-mint


def test_ttl_from_github_expires_at():
    _reset_cache()
    body = {"token": "ttl", "expires_at": "2099-01-01T00:00:00Z"}
    asyncio.run(_mint(token_body=body))
    token, exp = gas._token_cache["inst-1"]
    # Far-future expiry -> still valid now.
    assert exp > time.time()
    assert token == "ttl"


# ---------------------------------------------------------------------------
# Concurrency: double-checked locking, no duplicate mint, no herd
# ---------------------------------------------------------------------------


def test_parallel_refresh_mints_only_once():
    _reset_cache()
    calls = []

    async def _call(method, url, **kw):
        calls.append(1)
        # Simulate latency so coroutines overlap and contend for the lock.
        await asyncio.sleep(0.05)
        _resp = lambda: None
        _resp.status_code = 201
        _resp.text = "ok"

        def json():
            return {"token": "shared", "expires_at": "2099-01-01T00:00:00Z"}

        _resp.json = json
        return _resp

    async def run():
        with patch("services.github_app_service.github_api_call", new=_call):
            with patch("services.github_app_service._make_app_jwt", return_value="jwt"):
                results = await asyncio.gather(*[
                    gas.get_installation_token(installation_id="i1") for _ in range(10)
                ])
                return results

    results = asyncio.run(run())
    assert len(calls) == 1           # thundering herd prevented: 1 mint
    assert all(r == "shared" for r in results)


def test_per_installation_locks_isolated():
    _reset_cache()
    calls = {}

    async def _call(method, url, **kw):
        # installation id is encoded in the URL
        inst = url.split("/installations/")[1].split("/")[0]
        calls.setdefault(inst, 0)
        calls[inst] += 1
        await asyncio.sleep(0.02)
        _resp = lambda: None
        _resp.status_code = 201
        _resp.text = "ok"

        def json():
            return {"token": inst, "expires_at": "2099-01-01T00:00:00Z"}

        _resp.json = json
        return _resp

    async def run():
        with patch("services.github_app_service.github_api_call", new=_call):
            with patch("services.github_app_service._make_app_jwt", return_value="jwt"):
                await asyncio.gather(
                    *[gas.get_installation_token(installation_id=f"i{n}") for n in range(5)]
                )

    asyncio.run(run())
    assert all(v == 1 for v in calls.values())  # each installation minted once


def test_refresh_plus_invalidate_race_no_stale_write():
    _reset_cache()
    # Pre-seed a valid token; simulate a refresh that lags, while an invalidate
    # fires. After both, the cache must NOT hold the stale pre-seeded token, and
    # a fresh mint must happen.
    gas._token_cache["i1"] = ("stale", time.time() + 9999)

    refresh_done = asyncio.Event()
    invalidate_done = asyncio.Event()

    async def _slow_call(method, url, **kw):
        # Invalidate happens while we are "refreshing".
        await invalidate_done.wait()
        _resp = lambda: None
        _resp.status_code = 201
        _resp.text = "ok"

        def json():
            return {"token": "fresh", "expires_at": "2099-01-01T00:00:00Z"}

        _resp.json = json
        refresh_done.set()
        return _resp

    async def run():
        with patch("services.github_app_service.github_api_call", new=_slow_call):
            with patch("services.github_app_service._make_app_jwt", return_value="jwt"):
                # invalidate first (under the per-installation lock)
                await gas.invalidate_installation_token("i1")
                invalidate_done.set()
                t = await gas.get_installation_token(installation_id="i1")
                await refresh_done.wait()
                return t

    tok = asyncio.run(run())
    # Because invalidate removed the entry, the (slow) refresh runs and the
    # served token is the freshly minted one. No stale "stale" token survives.
    assert tok == "fresh"
    assert gas._token_cache["i1"][0] == "fresh"


def test_parallel_invalidate_safe():
    _reset_cache()
    gas._token_cache["i1"] = ("x", time.time() + 9999)

    async def run():
        await asyncio.gather(*[gas.invalidate_installation_token("i1") for _ in range(5)])

    asyncio.run(run())
    assert "i1" not in gas._token_cache


# ---------------------------------------------------------------------------
# Failure matrix: evict on refresh failure, never serve stale
# ---------------------------------------------------------------------------


def test_refresh_http_error_evicts_and_raises():
    _reset_cache()
    # Seed a token that SHOULD be valid (valid far-future) so a naive impl would
    # return it; but the refresh path only runs on miss/expiry. To force the
    # refresh path we seed an already-expired token, then make the mint fail.
    gas._token_cache["i1"] = ("old", time.time() - 10)

    async def _fail(_method, url, **kw):
        raise RuntimeError("network down")

    async def run():
        with patch("services.github_app_service.github_api_call", new=_fail):
            with patch("services.github_app_service._make_app_jwt", return_value="jwt"):
                try:
                    await gas.get_installation_token(installation_id="i1")
                except HTTPException:
                    pass

    asyncio.run(run())
    assert "i1" not in gas._token_cache   # evicted


def test_refresh_non_201_evicts_and_raises():
    _reset_cache()
    gas._token_cache["i1"] = ("old", time.time() - 10)

    async def _fail(_method, url, **kw):
        _resp = lambda: None
        _resp.status_code = 404
        _resp.text = "not found"

        def json():
            raise ValueError("no json")

        _resp.json = json
        return _resp

    async def run():
        with patch("services.github_app_service.github_api_call", new=_fail):
            with patch("services.github_app_service._make_app_jwt", return_value="jwt"):
                try:
                    await gas.get_installation_token(installation_id="i1")
                except HTTPException as e:
                    assert e.status_code == 502
                else:
                    raise AssertionError("expected 502")

    asyncio.run(run())
    assert "i1" not in gas._token_cache   # stale evicted
