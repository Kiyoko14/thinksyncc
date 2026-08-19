"""Regression tests for the async Supabase adapter (Production Hardening D2).

These prove:
  * ``get_supabase_async()`` exposes the same fluent builder interface.
  * ``.execute()`` on the async proxy is a coroutine that runs the blocking
    call off the event loop (in a worker thread), so the loop stays free.
  * A synchronous ``get_supabase()`` still works unchanged for sync callers.

Uses ``asyncio.run`` inside plain ``def`` tests to match the project's test
conventions (pytest-asyncio is not a dependency).
"""
import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from core import database as db


class _FakeResponse:
    def __init__(self, data):
        self.data = data
        self.error = None


class _FakeBuilder:
    """Minimal stand-in for a Supabase query builder."""

    def __init__(self, label="builder"):
        self._label = label

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def maybe_single(self, *a, **k):
        return self

    def execute(self):
        return _FakeResponse(["row"])


class _FakeClient:
    def table(self, *a, **k):
        return _FakeBuilder()

    def rpc(self, *a, **k):
        return _FakeBuilder()

    def from_(self, *a, **k):
        return _FakeBuilder()


@pytest.fixture(autouse=True)
def _patch_client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(db, "_client", fake)
    yield


def test_sync_client_still_works():
    # Synchronous callers keep the blocking behaviour, unchanged.
    client = db.get_supabase()
    res = client.table("jobs").select("*").limit(1).execute()
    assert res.data == ["row"]


def test_async_proxy_exposes_same_interface():
    proxy = asyncio.run(db.get_supabase_async())
    assert isinstance(proxy, db._AsyncClient)
    assert isinstance(proxy.table("x"), db._AsyncBuilder)
    assert isinstance(proxy.rpc("fn"), db._AsyncBuilder)
    assert isinstance(proxy.from_("x"), db._AsyncBuilder)


def test_async_execute_is_coroutine_and_offloads_to_thread():
    async def scenario():
        proxy = await db.get_supabase_async()
        builder = proxy.table("jobs").select("*").eq("id", "1").limit(1)
        coro = builder.execute()
        assert asyncio.iscoroutine(coro)

        loop_thread = threading.current_thread()
        recorded = {}
        b = proxy.table("jobs").select("*")
        orig_execute = b._builder.execute

        def _tracked():
            recorded["thread"] = threading.current_thread()
            return orig_execute()

        b._builder.execute = _tracked
        res = await b.execute()
        assert res.data == ["row"]
        assert recorded["thread"] is not None
        assert recorded["thread"] is not loop_thread
        return res

    result = asyncio.run(scenario())
    assert result.data == ["row"]


def test_async_proxy_chains_maybe_single_then_execute():
    async def scenario():
        proxy = await db.get_supabase_async()
        return await proxy.table("workspaces").select("user_id").eq("id", "w").maybe_single().execute()

    res = asyncio.run(scenario())
    assert res.data == ["row"]


def test_to_thread_executor_does_not_block_event_loop():
    """Even with a slow blocking execute, the loop can do other work."""
    slow = _FakeClient()

    class _SlowBuilder(_FakeBuilder):
        def execute(self):
            import time

            time.sleep(0.05)
            return _FakeResponse(["slow"])

    slow.table = lambda *a, **k: _SlowBuilder()
    slow.rpc = lambda *a, **k: _SlowBuilder()
    db._client = slow

    async def scenario():
        done = []

        async def quick():
            done.append("quick")

        proxy = await db.get_supabase_async()
        t = asyncio.create_task(quick())
        res = await proxy.table("jobs").select("*").execute()
        await t
        assert res.data == ["slow"]
        assert "quick" in done

    asyncio.run(scenario())
