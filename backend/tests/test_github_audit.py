"""Part 7 — Observability & Structured Audit tests.

Verifies:
  - AuditEvent model + record_github_event single entry point
  - Secret redaction (key-based AND value-based: Authorization/Bearer/ghp_/PEM)
  - Correlation contextvar threading (continue-or-mint rule)
  - Extended columns persisted (workspace_id, github_connection_id, server_id,
    request_id, correlation_id, step_name, duration_ms, status, repo_id,
    repo_full_name)
  - Best-effort: audit write failure must NOT raise
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.github_audit import (
    AuditEvent,
    get_correlation_id,
    record_github_event,
    reset_correlation,
    set_correlation_id,
    set_request_id,
)


class _FakeDB:
    def __init__(self):
        self.inserts = []

    def table(self, name):
        return self

    def insert(self, record):
        self.inserts.append(record)
        return self

    async def execute(self):
        return type("R", (), {"data": [self.inserts[-1]]})()


def _fake_supabase(monkeypatch, db):
    async def _get():
        return _FakeSupa(db)

    class _FakeSupa:
        def __init__(self, d):
            self._d = d

        def table(self, name):
            self._name = name
            return self

        def insert(self, record):
            self._record = record
            return self

        async def execute(self):
            self._d.inserts.append(record := self._record)
            return type("R", (), {"data": [record]})()

    monkeypatch.setattr("services.github_audit.get_supabase_async", _get)
    return db


import pytest


@pytest.fixture
def db(monkeypatch):
    d = _FakeDB()
    _fake_supabase(monkeypatch, d)
    reset_correlation()
    yield d
    reset_correlation()


def test_audit_event_persists_extended_columns(db):
    event = AuditEvent(
        event_type="github.rate_limit",
        installation_id="123",
        workspace_id="ws-1",
        github_connection_id="conn-1",
        server_id="srv-1",
        user_id="u-1",
        step_name="github_api_call",
        duration_ms=42,
        status="waiting",
        repo_id=555,
        repo_full_name="o/r",
        metadata={"retry_after": 10},
    )
    asyncio.get_event_loop().run_until_complete(record_github_event(event))
    rec = db.inserts[-1]
    assert rec["event_type"] == "github.rate_limit"
    assert rec["installation_id"] == "123"
    assert rec["workspace_id"] == "ws-1"
    assert rec["github_connection_id"] == "conn-1"
    assert rec["server_id"] == "srv-1"
    assert rec["user_id"] == "u-1"
    assert rec["step_name"] == "github_api_call"
    assert rec["duration_ms"] == 42
    assert rec["status"] == "waiting"
    assert rec["repo_id"] == 555
    assert rec["repo_full_name"] == "o/r"
    assert rec["metadata"] == {"retry_after": 10}


def test_correlation_continue_or_mint_rule(db):
    # --- Part A: no correlation set -> mint ---
    reset_correlation()
    set_correlation_id(None)
    assert get_correlation_id().startswith("corr_")  # minted on set
    asyncio.get_event_loop().run_until_complete(
        record_github_event(AuditEvent(event_type="x", correlation_id=None))
    )
    first = db.inserts[-1]["correlation_id"]
    assert first.startswith("corr_")

    # --- Part B: explicit set, then a second set is IGNORED (continue rule) ---
    reset_correlation()
    set_correlation_id("explicit-1")
    set_correlation_id("explicit-2")  # ignored because already present
    assert get_correlation_id() == "explicit-1"
    asyncio.get_event_loop().run_until_complete(
        record_github_event(AuditEvent(event_type="y"))
    )
    assert db.inserts[-1]["correlation_id"] == "explicit-1"


def test_request_id_threading(db):
    set_request_id("req-xyz")
    asyncio.get_event_loop().run_until_complete(
        record_github_event(AuditEvent(event_type="z"))
    )
    assert db.inserts[-1]["request_id"] == "req-xyz"


def test_key_based_redaction(db):
    event = AuditEvent(
        event_type="x",
        metadata={"token": "ghp_secretvalue", "note": "ok", "ssh_private_key": "PRIVATE"},
    )
    asyncio.get_event_loop().run_until_complete(record_github_event(event))
    meta = db.inserts[-1]["metadata"]
    assert meta["token"] == "[REDACTED]"
    assert meta["ssh_private_key"] == "[REDACTED]"
    assert meta["note"] == "ok"


def test_value_based_redaction(db):
    event = AuditEvent(
        event_type="x",
        metadata={"auth": "Bearer abc123token", "url": "https://api?x=1"},
    )
    asyncio.get_event_loop().run_until_complete(record_github_event(event))
    meta = db.inserts[-1]["metadata"]
    assert meta["auth"] == "[REDACTED]"
    assert meta["url"] == "https://api?x=1"  # not a secret value pattern


def test_github_token_value_redaction(db):
    event = AuditEvent(
        event_type="x",
        metadata={"body": "token=ghp_abcdefghijklmnopqrstuvwxyz012345"},
    )
    asyncio.get_event_loop().run_until_complete(record_github_event(event))
    redacted = db.inserts[-1]["metadata"]["body"]
    assert "ghp_" not in redacted
    assert redacted == "token=[REDACTED]"


def test_audit_write_failure_does_not_raise(db, monkeypatch):
    async def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("services.github_audit.get_supabase_async", _boom)
    # Must not raise
    asyncio.get_event_loop().run_until_complete(
        record_github_event(AuditEvent(event_type="x", metadata={"a": 1}))
    )
