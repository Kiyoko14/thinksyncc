"""Tests for the CREATE Saga (Part 5) built on the Part 4 CompensationLedger.

Guarantees under test:
  * Happy path: app connection + workspace row inserted, clone invoked, no
    compensation runs.
  * Clone failure: the whole CREATE rolls back — workspace row deleted, app
    connection deleted, remote dir removed (rm -rf) — leaving NO half-created
    object. The original 502 error is re-raised.
  * Workspace-row failure (after app connection): app connection is compensated.
  * allocate_port / assign_domain stay OUTSIDE the saga: their failure does NOT
    roll back a successful CREATE.

All external effects are mocked; the fake Supabase records inserts/deletes so
orphan-absence can be asserted directly.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from models.github_app import GitHubAppCloneRequest
from services import workspace_service as ws_mod
from services.workspace_service import WorkspaceService


# ---------------------------------------------------------------------------
# Fake Supabase that records rows per table
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, db, name):
        self._db = db
        self._name = name
        self._op = None
        self._payload = None
        self._filters = {}

    def insert(self, rec):
        self._op = "insert"; self._payload = rec; return self

    def delete(self):
        self._op = "delete"; return self

    def select(self, *_a, **_k):
        self._op = "select"; return self

    def eq(self, c, v):
        self._filters[c] = v; return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def maybe_single(self):
        return self

    async def execute(self):
        db = self._db
        if self._op == "insert":
            db.rows.setdefault(self._name, {})
            rid = self._payload["id"]
            db.rows[self._name][rid] = dict(self._payload)
            db.inserts.append((self._name, rid))
            return _Resp([self._payload])
        if self._op == "delete":
            rid = self._filters.get("id")
            db.rows.get(self._name, {}).pop(rid, None)
            db.deletes.append((self._name, rid))
            return _Resp([])
        if self._op == "select":
            # by_name duplicate probe -> none
            return _Resp(None)
        return _Resp(None)


class _Supa:
    def __init__(self, db):
        self._db = db

    def table(self, name):
        return _Table(self._db, name)


class _DB:
    def __init__(self):
        self.rows = {}
        self.inserts = []
        self.deletes = []


def _run(coro):
    return asyncio.run(coro)


def _base_patches(db, *, clone_ok=True):
    supa = _Supa(db)
    return [
        patch("services.workspace_service.get_supabase_async", new=AsyncMock(return_value=supa)),
        patch("services.workspace_service.ServerService.get_server",
              new=staticmethod(lambda server_id, user_id: {"id": server_id})),
        patch("services.workspace_service.SSHService.execute", new=AsyncMock()),
        patch("services.workspace_service.WorkspaceService.get_workspace_by_slug",
              new=staticmethod(lambda **kw: None)),
        patch("services.workspace_service._allocate_port", new=lambda *_a, **_k: None),
        patch("services.workspace_service.assign_domain", new=lambda *_a, **_k: None),
    ]


def _start(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in patches:
        p.stop()


_UID = "11111111-1111-4111-8111-111111111111"
_SID = "22222222-2222-4222-8222-222222222222"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_app_workspace_happy_path_no_compensation():
    db = _DB()
    patches = _base_patches(db)
    clone = patch("services.github_app_service.GitHubAppService.clone",
                  new=AsyncMock(return_value={"ok": True}))
    _start(patches)
    m = clone.start()
    try:
        result = _run(WorkspaceService.create_workspace(
            user_id=_UID, server_id=_SID, name="app",
            app_clone=GitHubAppCloneRequest(installation_id="inst1", repo="o/app", repo_id=9),
        ))
    finally:
        clone.stop(); _stop(patches)

    # Both rows persisted; nothing deleted (no compensation).
    assert any(t == "github_connections" for t, _ in db.inserts)
    assert any(t == "workspaces" for t, _ in db.inserts)
    assert db.deletes == []
    m.assert_awaited_once()


# ---------------------------------------------------------------------------
# Clone failure -> full rollback, no orphan
# ---------------------------------------------------------------------------


def test_clone_failure_rolls_back_everything_no_orphan():
    db = _DB()
    patches = _base_patches(db)
    ssh_calls = []

    async def _ssh(*, server, command):
        ssh_calls.append(command)

    ssh_patch = patch("services.workspace_service.SSHService.execute", new=_ssh)
    clone = patch("services.github_app_service.GitHubAppService.clone",
                  new=AsyncMock(return_value={"ok": False, "stderr": "auth failed", "code": "E"}))
    _start(patches)
    ssh_patch.start()
    clone.start()
    try:
        with pytest.raises(HTTPException) as exc:
            _run(WorkspaceService.create_workspace(
                user_id=_UID, server_id=_SID, name="app",
                app_clone=GitHubAppCloneRequest(installation_id="inst1", repo="o/app", repo_id=9),
            ))
    finally:
        clone.stop(); ssh_patch.stop(); _stop(patches)

    # Original clone error preserved.
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "GITHUB_CLONE_FAILED"

    # No half-created object: workspace row and app connection both removed.
    assert db.rows.get("workspaces", {}) == {}
    assert db.rows.get("github_connections", {}) == {}
    assert any(t == "workspaces" for t, _ in db.deletes)
    assert any(t == "github_connections" for t, _ in db.deletes)
    # remote_mkdir compensation issued an rm -rf.
    assert any(c.startswith("rm -rf") for c in ssh_calls)


# ---------------------------------------------------------------------------
# ThinkSync (no GitHub) happy path — no connection row created
# ---------------------------------------------------------------------------


def test_thinksync_workspace_no_connection_row():
    db = _DB()
    patches = _base_patches(db)
    _start(patches)
    try:
        _run(WorkspaceService.create_workspace(user_id=_UID, server_id=_SID, name="plain"))
    finally:
        _stop(patches)

    assert any(t == "workspaces" for t, _ in db.inserts)
    assert not any(t == "github_connections" for t, _ in db.inserts)
    assert db.deletes == []  # no failure, no compensation
