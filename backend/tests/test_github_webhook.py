"""Tests for GitHub App webhook infrastructure (Part 1).

Covers:
  * valid signature accepted
  * invalid signature rejected (401)
  * missing signature rejected (401)
  * replay protection (duplicate X-GitHub-Delivery skipped)
  * installation.created / deleted / suspend / unsuspend
  * repository.deleted / renamed (mapped by canonical repo_id)
  * audit event written
  * cache invalidation invoked on deleted/suspend
  * secret redaction in the audit helper

All DB access is mocked with an in-memory fake that records rows per table so
assertions can inspect exactly what the handlers wrote.
"""

import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest

from services import github_webhook_service as wh
from services.github_webhook_service import WebhookError, process_webhook, verify_signature


_SECRET = "test-webhook-secret"


# ---------------------------------------------------------------------------
# In-memory fake Supabase
# ---------------------------------------------------------------------------


class _FakeDB:
    """Records inserts/updates/selects per table for assertions."""

    def __init__(self, existing_deliveries=None, connections=None, installations=None):
        self.deliveries = set(existing_deliveries or [])
        self.connections = connections if connections is not None else []
        self.installations = installations if installations is not None else []
        self.audit = []
        self.updates = []  # (table, patch, filters)
        self.inserts = []  # (table, record)


class _Table:
    def __init__(self, db, name):
        self._db = db
        self._name = name
        self._op = None
        self._payload = None
        self._filters = {}
        self._maybe_single = False

    def insert(self, rec):
        self._op = "insert"
        self._payload = rec
        return self

    def upsert(self, rec, **_k):
        self._op = "upsert"
        self._payload = rec
        return self

    def update(self, patch):
        self._op = "update"
        self._payload = patch
        return self

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def maybe_single(self):
        self._maybe_single = True
        return self

    async def execute(self):
        db = self._db
        if self._op in ("insert", "upsert"):
            db.inserts.append((self._name, self._payload))
            if self._name == "github_webhook_deliveries":
                db.deliveries.add(self._payload["delivery_id"])
            if self._name == "github_audit_log":
                db.audit.append(self._payload)
            if self._name == "github_app_installations":
                db.installations.append(self._payload)
            return _Resp(self._payload)
        if self._op == "update":
            db.updates.append((self._name, self._payload, dict(self._filters)))
            return _Resp([])
        # select
        if self._name == "github_webhook_deliveries":
            did = self._filters.get("delivery_id")
            data = {"delivery_id": did} if did in db.deliveries else None
            return _Resp(data if self._maybe_single else ([] if data is None else [data]))
        if self._name == "github_app_installations":
            iid = self._filters.get("id")
            row = next((r for r in db.installations if str(r.get("id")) == str(iid)), None)
            return _Resp(row if self._maybe_single else ([] if row is None else [row]))
        return _Resp(None if self._maybe_single else [])


class _Resp:
    def __init__(self, data):
        self.data = data


class _Supa:
    def __init__(self, db):
        self._db = db

    def table(self, name):
        return _Table(self._db, name)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


class _Settings:
    GITHUB_APP_WEBHOOK_SECRET = _SECRET


def _run(coro):
    return asyncio.run(coro)


def _process(payload, event, delivery, *, db, sign=True, bad_sig=False):
    raw = json.dumps(payload).encode()
    if bad_sig:
        sig = "sha256=deadbeef"
    elif sign:
        sig = _sign(raw)
    else:
        sig = None
    supa = _Supa(db)
    with patch("services.github_webhook_service.get_settings", return_value=_Settings()):
        with patch("services.github_webhook_service.get_supabase_async", new=AsyncMock(return_value=supa)):
            with patch("services.github_audit.get_supabase_async", new=AsyncMock(return_value=supa)):
                with patch("services.github_webhook_service._invalidate_token", new=AsyncMock()) as inv:
                    result = _run(
                        process_webhook(
                            raw_body=raw,
                            signature_header=sig,
                            event_type=event,
                            delivery_id=delivery,
                            payload=payload,
                        )
                    )
                    return result, inv


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_valid_signature_accepted():
    body = b'{"action":"created"}'
    with patch("services.github_webhook_service.get_settings", return_value=_Settings()):
        verify_signature(raw_body=body, signature_header=_sign(body))  # no raise


def test_invalid_signature_rejected():
    body = b'{"action":"created"}'
    with patch("services.github_webhook_service.get_settings", return_value=_Settings()):
        with pytest.raises(WebhookError) as exc:
            verify_signature(raw_body=body, signature_header="sha256=bad")
    assert exc.value.status_code == 401
    assert exc.value.code == "INVALID_SIGNATURE"


def test_missing_signature_rejected():
    with patch("services.github_webhook_service.get_settings", return_value=_Settings()):
        with pytest.raises(WebhookError) as exc:
            verify_signature(raw_body=b"{}", signature_header=None)
    assert exc.value.status_code == 401
    assert exc.value.code == "MISSING_SIGNATURE"


def test_bad_signature_via_process_returns_error():
    db = _FakeDB()
    with pytest.raises(WebhookError) as exc:
        _process({"action": "created"}, "installation", "d1", db=db, bad_sig=True)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------


def test_replay_is_skipped():
    db = _FakeDB(existing_deliveries={"dup-1"})
    payload = {"action": "created", "installation": {"id": 99, "account": {"login": "x", "id": 1}}}
    result, _ = _process(payload, "installation", "dup-1", db=db)
    assert result["status"] == "replay"
    # No new installation insert happened on replay.
    assert all(t != "github_app_installations" for t, _ in db.inserts)


def test_first_delivery_records_delivery_id():
    db = _FakeDB()
    payload = {"action": "created", "installation": {"id": 5, "account": {"login": "o", "id": 2}}}
    result, _ = _process(payload, "installation", "fresh-1", db=db)
    assert result["status"] == "processed"
    assert "fresh-1" in db.deliveries


# ---------------------------------------------------------------------------
# Installation lifecycle
# ---------------------------------------------------------------------------


def test_installation_created_upserts():
    db = _FakeDB()
    payload = {"action": "created", "installation": {"id": 10, "account": {"login": "acme", "id": 7}, "permissions": {"contents": "read"}}}
    _process(payload, "installation", "c1", db=db)
    ins = [r for t, r in db.inserts if t == "github_app_installations"]
    assert ins and ins[0]["id"] == "10"
    assert ins[0]["status"] == "active"
    assert any(a["event_type"] == "installation.created" for a in db.audit)


def test_installation_deleted_soft_and_invalidates_cache():
    db = _FakeDB()
    payload = {"action": "deleted", "installation": {"id": 11}}
    result, inv = _process(payload, "installation", "d2", db=db)
    # SOFT delete -> status update, not row removal.
    upd = [(p, f) for t, p, f in db.updates if t == "github_app_installations"]
    assert any(p.get("status") == "deleted" for p, _ in upd)
    inv.assert_called_once_with("11")
    assert any(a["event_type"] == "installation.deleted" for a in db.audit)


def test_installation_suspend_invalidates_cache():
    db = _FakeDB()
    payload = {"action": "suspend", "installation": {"id": 12}}
    _, inv = _process(payload, "installation", "s1", db=db)
    upd = [p for t, p, f in db.updates if t == "github_app_installations"]
    assert any(p.get("status") == "suspended" for p in upd)
    inv.assert_called_once_with("12")


def test_installation_unsuspend_reactivates():
    db = _FakeDB()
    payload = {"action": "unsuspend", "installation": {"id": 13}}
    _, inv = _process(payload, "installation", "u1", db=db)
    upd = [p for t, p, f in db.updates if t == "github_app_installations"]
    assert any(p.get("status") == "active" for p in upd)
    # Part 6 D2: unsuspend invalidates the (suspended/unusable) token cache.
    inv.assert_called_once_with("13")


# ---------------------------------------------------------------------------
# Repository lifecycle (mapped by canonical repo_id)
# ---------------------------------------------------------------------------


def test_repository_deleted_marks_by_repo_id():
    db = _FakeDB()
    payload = {"action": "deleted", "repository": {"id": 555, "full_name": "o/repo"}}
    _process(payload, "repository", "r1", db=db)
    upd = [(p, f) for t, p, f in db.updates if t == "github_connections"]
    assert any(f.get("repo_id") == 555 for _, f in upd)
    assert any(a["event_type"] == "repository.deleted" for a in db.audit)


def test_repository_renamed_updates_full_name_by_repo_id():
    db = _FakeDB()
    payload = {"action": "renamed", "repository": {"id": 777, "full_name": "o/new-name"}, "changes": {"repository": {"name": {"from": "old-name"}}}}
    _process(payload, "repository", "r2", db=db)
    upd = [(p, f) for t, p, f in db.updates if t == "github_connections"]
    # Mapped by immutable repo_id; only full_name changes.
    assert any(f.get("repo_id") == 777 and p.get("repo_full_name") == "o/new-name" for p, f in upd)
    assert any(a["event_type"] == "repository.renamed" for a in db.audit)


# ---------------------------------------------------------------------------
# Unknown event is idempotently ignored
# ---------------------------------------------------------------------------


def test_unknown_event_ignored():
    db = _FakeDB()
    result, _ = _process({"action": "whatever"}, "push", "p1", db=db)
    assert result["status"] == "ignored"
    assert "p1" in db.deliveries


# ---------------------------------------------------------------------------
# Audit secret redaction
# ---------------------------------------------------------------------------


def test_audit_redacts_secrets():
    from services.github_audit import _redact

    dirty = {
        "token": "ghs_secret",
        "private_key": "-----BEGIN-----",
        "nested": {"client_secret": "abc", "safe": "ok"},
        "list": [{"authorization": "Bearer x"}],
        "repo_full_name": "o/r",
    }
    clean = _redact(dirty)
    assert clean["token"] == "[REDACTED]"
    assert clean["private_key"] == "[REDACTED]"
    assert clean["nested"]["client_secret"] == "[REDACTED]"
    assert clean["nested"]["safe"] == "ok"
    assert clean["list"][0]["authorization"] == "[REDACTED]"
    assert clean["repo_full_name"] == "o/r"
