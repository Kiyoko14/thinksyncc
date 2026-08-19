"""Tests for the Workspace Lifecycle Orchestrator (Part 4).

Covers:
  * CompensationLedger: StepResult fields, ordered execution, reverse-order
    compensation on failure, primary-error preservation, compensation-failure
    isolation, execute-once / compensate-only-if-succeeded determinism.
  * DELETE transition: row delete + remote cleanup + orphan app-connection
    cleanup + audit + cache invalidation.
  * DELETE compensation: remote cleanup failure restores the workspace row and
    re-raises the original error.
  * DISCONNECT transition: connection unlinked, orphan app-connection removed.
  * remote path guard rejects unsafe paths.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services import workspace_lifecycle as wl
from services.workspace_lifecycle import (
    CompensationLedger,
    CompensationStep,
    LifecycleAction,
    LifecycleContext,
    StepResult,
    WorkspaceLifecycleOrchestrator,
    _assert_safe_workspace_path,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Ledger mechanics
# ---------------------------------------------------------------------------


def test_ledger_records_stepresult_fields():
    ledger = CompensationLedger()

    async def _ok():
        return {"x": 1}

    _run(ledger.run(CompensationStep(name="s1", execute=_ok, metadata={"k": "v"})))
    assert len(ledger.results) == 1
    r = ledger.results[0]
    assert isinstance(r, StepResult)
    assert r.success is True
    assert r.started_at is not None and r.finished_at is not None
    assert r.duration is not None and r.duration >= 0
    assert r.metadata["k"] == "v"
    assert r.error is None


def test_ledger_compensates_in_reverse_on_failure():
    ledger = CompensationLedger()
    order = []

    async def _e1():
        order.append("e1")

    async def _c1():
        order.append("c1")

    async def _e2():
        order.append("e2")

    async def _c2():
        order.append("c2")

    async def _e3_fail():
        order.append("e3")
        raise RuntimeError("boom")

    async def run():
        await ledger.run(CompensationStep(name="s1", execute=_e1, compensate=_c1))
        await ledger.run(CompensationStep(name="s2", execute=_e2, compensate=_c2))
        await ledger.run(CompensationStep(name="s3", execute=_e3_fail, compensate=None))

    with pytest.raises(RuntimeError, match="boom"):
        _run(run())

    # e1, e2 succeeded; e3 failed. Compensation runs c2 then c1 (reverse).
    assert order == ["e1", "e2", "e3", "c2", "c1"]


def test_primary_error_preserved_even_if_compensation_fails():
    ledger = CompensationLedger()

    async def _e1():
        return "ok"

    async def _c1_fail():
        raise ValueError("compensation blew up")

    async def _e2_fail():
        raise RuntimeError("primary")

    async def run():
        await ledger.run(CompensationStep(name="s1", execute=_e1, compensate=_c1_fail))
        await ledger.run(CompensationStep(name="s2", execute=_e2_fail))

    # The PRIMARY error surfaces, not the compensation error.
    with pytest.raises(RuntimeError, match="primary"):
        _run(run())

    # Compensation failure is recorded separately.
    assert any(not c.success and "compensation blew up" in (c.error or "")
               for c in ledger.compensation_results)


def test_compensate_only_for_succeeded_steps():
    ledger = CompensationLedger()
    compensated = []

    async def _e_ok():
        return "ok"

    async def _c_ok():
        compensated.append("ok_step")

    async def _e_fail():
        raise RuntimeError("x")

    async def _c_should_not_run():
        compensated.append("failed_step")

    async def run():
        await ledger.run(CompensationStep(name="ok", execute=_e_ok, compensate=_c_ok))
        await ledger.run(CompensationStep(name="fail", execute=_e_fail, compensate=_c_should_not_run))

    with pytest.raises(RuntimeError):
        _run(run())

    # Only the successfully-executed step is compensated; the failed step is not.
    assert compensated == ["ok_step"]


# ---------------------------------------------------------------------------
# Path guard
# ---------------------------------------------------------------------------


def test_path_guard_rejects_root_and_escape():
    with patch("services.workspace_service.WorkspaceService._workspaces_root", return_value="/root/workspaces"):
        # root itself
        with pytest.raises(ValueError):
            _assert_safe_workspace_path("/root/workspaces")
        # escape
        with pytest.raises(ValueError):
            _assert_safe_workspace_path("/etc/passwd")
        # traversal
        with pytest.raises(ValueError):
            _assert_safe_workspace_path("/root/workspaces/../secret")
        # empty
        with pytest.raises(ValueError):
            _assert_safe_workspace_path("")
        # valid path does not raise
        _assert_safe_workspace_path("/root/workspaces/abc123")


# ---------------------------------------------------------------------------
# Fake Supabase for transition tests
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
        self._maybe_single = False

    def insert(self, rec):
        self._op = "insert"; self._payload = rec; return self

    def update(self, patch):
        self._op = "update"; self._payload = patch; return self

    def delete(self):
        self._op = "delete"; return self

    def select(self, *_a, **_k):
        self._op = "select"; return self

    def eq(self, col, val):
        self._filters[col] = val; return self

    def maybe_single(self):
        self._maybe_single = True; return self

    async def execute(self):
        db = self._db
        db.ops.append((self._name, self._op, self._payload, dict(self._filters)))
        if self._name == "github_connections" and self._op == "select":
            cid = self._filters.get("id")
            row = db.connections.get(cid)
            return _Resp(row if self._maybe_single else ([row] if row else []))
        if self._name == "workspaces" and self._op == "select":
            # "other workspaces referencing connection" query
            conn_id = self._filters.get("github_connection_id")
            refs = [w for w in db.workspaces.values()
                    if w.get("github_connection_id") == conn_id]
            return _Resp(refs)
        if self._name == "workspaces" and self._op == "update":
            wid = self._filters.get("id")
            if wid in db.workspaces:
                db.workspaces[wid].update(self._payload)
            return _Resp([db.workspaces.get(wid)])
        if self._name == "workspaces" and self._op == "insert":
            rec = self._payload
            db.workspaces[rec["id"]] = dict(rec)
            return _Resp([rec])
        if self._name == "workspaces" and self._op == "delete":
            db.workspaces.pop(self._filters.get("id"), None)
            return _Resp([])
        if self._name == "github_connections" and self._op == "delete":
            db.connections.pop(self._filters.get("id"), None)
            db.deleted_connections.append(self._filters.get("id"))
            return _Resp([])
        return _Resp([])


class _Supa:
    def __init__(self, db):
        self._db = db

    def table(self, name):
        return _Table(self._db, name)


class _DB:
    def __init__(self, workspaces, connections):
        self.workspaces = {w["id"]: w for w in workspaces}
        self.connections = connections
        self.ops = []
        self.deleted_connections = []
        self.deleting_id = None


def _patch_common(db, workspace_row, ssh=None):
    """Patch orchestrator dependencies for a transition test."""
    supa = _Supa(db)
    patches = [
        patch("services.workspace_lifecycle._supa", new=AsyncMock(return_value=supa)),
        patch("services.workspace_service.WorkspaceService.get_workspace_by_id",
              new=staticmethod(lambda id, user_id: dict(workspace_row))),
        patch("services.github_audit.record_github_event", new=AsyncMock()),
        patch("services.workspace_lifecycle._resolve_server",
              new=AsyncMock(return_value={"id": "srv1"})),
        patch("services.ssh_service.SSHService.execute", new=(ssh or AsyncMock())),
        patch("services.workspace_service.WorkspaceService._workspaces_root",
              return_value="/root/workspaces"),
    ]
    return patches, supa


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


# ---------------------------------------------------------------------------
# DELETE transition
# ---------------------------------------------------------------------------


def test_delete_transition_removes_workspace_and_orphan_connection():
    ws = {
        "id": "ws1", "user_id": "u1", "server_id": "srv1",
        "path": "/root/workspaces/ws1abc", "github_connection_id": "conn1",
        "name": "app", "slug": "app-x", "domain": "d",
    }
    conn = {"id": "conn1", "auth_method": "app", "installation_id": "inst1",
            "repo_id": 5, "repo_full_name": "o/r"}
    db = _DB([ws], {"conn1": conn})
    patches, supa = _patch_common(db, ws)
    inv = patch("services.github_app_service.invalidate_installation_token", new=AsyncMock())

    _enter(patches)
    m = inv.start()
    try:
        result = _run(WorkspaceLifecycleOrchestrator.transition(
            ctx=LifecycleContext(action=LifecycleAction.DELETE, workspace_id="ws1", user_id="u1")
        ))
    finally:
        inv.stop()
        _exit(patches)

    assert result.success is True
    assert "ws1" not in db.workspaces           # workspace row deleted
    assert "conn1" in db.deleted_connections    # orphan app connection removed
    m.assert_called_once_with("inst1")          # cache invalidated


def test_delete_compensation_restores_row_on_remote_failure():
    ws = {
        "id": "ws1", "user_id": "u1", "server_id": "srv1",
        "path": "/root/workspaces/ws1abc", "github_connection_id": None,
        "name": "n", "slug": "s", "domain": "d",
    }
    db = _DB([ws], {})
    # SSH remote rm fails -> remove_remote_dir step fails -> compensate restores row.
    failing_ssh = AsyncMock(side_effect=RuntimeError("ssh down"))
    patches, supa = _patch_common(db, ws, ssh=failing_ssh)

    _enter(patches)
    try:
        with pytest.raises(RuntimeError, match="ssh down"):
            _run(WorkspaceLifecycleOrchestrator.transition(
                ctx=LifecycleContext(action=LifecycleAction.DELETE, workspace_id="ws1", user_id="u1")
            ))
    finally:
        _exit(patches)

    # Compensation reinserted the workspace row (no orphan / no data loss).
    assert "ws1" in db.workspaces


# ---------------------------------------------------------------------------
# DISCONNECT transition
# ---------------------------------------------------------------------------


def test_disconnect_unlinks_and_cleans_orphan_connection():
    ws = {
        "id": "ws1", "user_id": "u1", "server_id": "srv1",
        "path": "/root/workspaces/ws1abc", "github_connection_id": "conn1",
        "name": "n", "slug": "s", "domain": "d",
    }
    conn = {"id": "conn1", "auth_method": "app", "installation_id": "inst1"}
    db = _DB([ws], {"conn1": conn})
    patches, supa = _patch_common(db, ws)

    _enter(patches)
    try:
        result = _run(WorkspaceLifecycleOrchestrator.transition(
            ctx=LifecycleContext(action=LifecycleAction.DISCONNECT, workspace_id="ws1", user_id="u1")
        ))
    finally:
        _exit(patches)

    assert result.success is True
    # An update setting github_connection_id=None was issued.
    assert any(name == "workspaces" and op == "update" and payload.get("github_connection_id") is None
               for (name, op, payload, filt) in db.ops)
    # Orphan app connection cleaned up.
    assert "conn1" in db.deleted_connections
