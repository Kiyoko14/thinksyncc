"""
Tests for the Git tool layer in services/tools.py.

These tests verify:
  1. Git tools require a git repository (non-repo returns a clear error).
  2. Git tools are registered in the dispatch table.
  3. Destructive git tools (git_reset, git_clean) are assessed as HIGH risk.
  4. The execute_tool dispatch passes structured args (message/mode/force) to git funcs.

Async helpers are run via asyncio.run inside plain ``def`` tests to match
the project's test conventions (pytest-asyncio is not a dependency).
"""

import asyncio
from unittest.mock import patch

import pytest

from models.agent import ToolName
from services.tools import (
    _TOOL_FN,
    execute_tool,
)
from services.tools import (
    _git_status, _git_diff, _git_branch, _git_commit,
    _git_restore, _git_reset, _git_clean,
)
from services.agent_service import _assess_risk as _assess_git_risk


# ---------------------------------------------------------------------------
# 1. Registration
# ---------------------------------------------------------------------------

def test_git_tools_registered_in_dispatch():
    for name in [
        "git_status", "git_diff", "git_branch", "git_commit",
        "git_restore", "git_reset", "git_clean",
    ]:
        assert ToolName(name) in _TOOL_FN, f"{name} missing from dispatch"


def test_git_function_implementations_exist():
    for fn in (_git_status, _git_diff, _git_branch, _git_commit,
               _git_restore, _git_reset, _git_clean):
        assert callable(fn)


# ---------------------------------------------------------------------------
# 2. Risk assessment
# ---------------------------------------------------------------------------

def test_destructive_git_tools_high_risk():
    assert _assess_git_risk("git_reset", {}) == "high"
    assert _assess_git_risk("git_clean", {}) == "high"


def test_non_destructive_git_tools_not_high():
    assert _assess_git_risk("git_status", {}) == "low"
    assert _assess_git_risk("git_commit", {}) == "low"
    assert _assess_git_risk("git_restore", {}) == "medium"


# ---------------------------------------------------------------------------
# 3. Repository guard
# ---------------------------------------------------------------------------

def test_git_tool_rejects_non_repo():
    """A git tool must refuse to run outside a git repo."""

    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        return {"stdout": "NO_GIT", "stderr": "", "code": 1}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            return await _git_status(
                server={}, workspace_path="/w", allow_write=True, timeout=10
            )

    res = asyncio.run(run())
    assert res["code"] == 1
    assert "not a git repository" in res["stderr"]


def test_git_status_runs_inside_repo():
    """git_status executes `git status` when .git is present."""

    captured = {}

    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        if "test -d .git" in command:
            return {"stdout": "GIT_OK", "stderr": "", "code": 0}
        captured["cmd"] = command
        return {"stdout": "On branch main", "stderr": "", "code": 0}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            return await _git_status(
                server={}, workspace_path="/w", allow_write=True, timeout=10
            )

    res = asyncio.run(run())
    assert res["code"] == 0
    assert "git status" in captured["cmd"]


# ---------------------------------------------------------------------------
# 4. Structured args dispatch via execute_tool
# ---------------------------------------------------------------------------

def test_execute_tool_passes_git_message():
    """git_commit receives the `message` arg from the plan step."""

    captured = {}

    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        if "test -d .git" in command:
            return {"stdout": "GIT_OK", "stderr": "", "code": 0}
        captured["cmd"] = command
        return {"stdout": "[main abc] msg", "stderr": "", "code": 0}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            return await execute_tool(
                tool_name="git_commit",
                args={"message": "deploy: v1.2"},
                intent="server",
                server={},
                workspace_path="/w",
                allow_write=True,
                timeout=10,
            )

    res = asyncio.run(run())
    assert res.exit_code == 0
    assert "deploy: v1.2" in captured["cmd"]


def test_execute_tool_git_clean_dry_run_by_default():
    """git_clean without force=true must NOT delete (dry-run)."""

    captured = {}

    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        if "test -d .git" in command:
            return {"stdout": "GIT_OK", "stderr": "", "code": 0}
        captured["cmd"] = command
        return {"stdout": "", "stderr": "", "code": 0}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            return await execute_tool(
                tool_name="git_clean",
                args={},
                intent="server",
                server={},
                workspace_path="/w",
                allow_write=True,
                timeout=10,
            )

    asyncio.run(run())
    assert "-fdx" not in captured["cmd"]
    assert "-ndx" in captured["cmd"]
