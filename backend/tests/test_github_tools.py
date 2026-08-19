"""Tests for the GitHub agent tools (github_pull / github_push) + risk mapping.

These tests verify the agent-facing GitHub foundation WITHOUT live network:
  1. github_pull / github_push are registered in the dispatch table.
  2. execute_tool rejects a github tool when github_connection_id is missing.
  3. github_pull refuses to run outside a git repo (reuses _require_git_repo).
  4. github_push refuses when allow_write=False (push gate).
  5. github_pull / github_push delegate to GitHubService.pull/push with the
     decrypted key (mocked) and report success/failure.
  6. _assess_risk: github_push == 'high', github_pull == 'medium'.
  7. _map_tool_to_approval_type: github_push -> DEPLOYMENT.

Async helpers run via asyncio.run inside plain ``def`` tests to match the
project's test conventions.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from models.agent import ToolName
from services.tools import (
    _TOOL_FN,
    execute_tool,
)
from services.tools import (
    _github_pull,
    _github_push,
)
from services.agent_service import _assess_risk, _map_tool_to_approval_type


# ---------------------------------------------------------------------------
# 1. Registration
# ---------------------------------------------------------------------------


def test_github_tools_registered():
    for name in ["github_pull", "github_push"]:
        assert ToolName(name) in _TOOL_FN, f"{name} missing from dispatch"


def test_github_functions_callable():
    assert callable(_github_pull)
    assert callable(_github_push)


# ---------------------------------------------------------------------------
# 2. Missing connection id is rejected
# ---------------------------------------------------------------------------


def test_github_pull_requires_connection_id():
    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        return {"stdout": "GIT_OK", "stderr": "", "code": 0}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            return await execute_tool(
                tool_name="github_pull",
                args={},  # no github_connection_id
                intent="server",
                server={"user_id": "u1"},
                workspace_path="/w",
                allow_write=True,
                timeout=10,
            )

    res = asyncio.run(run())
    assert res.exit_code == 1
    assert "github_connection_id" in res.stderr


# ---------------------------------------------------------------------------
# 3. repo-guard (reuses _require_git_repo)
# ---------------------------------------------------------------------------


def test_github_pull_rejects_non_repo():
    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        if "test -d .git" in command:
            return {"stdout": "NO_GIT", "stderr": "", "code": 1}
        return {"stdout": "", "stderr": "", "code": 0}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            return await execute_tool(
                tool_name="github_pull",
                args={"github_connection_id": "cid"},
                intent="server",
                server={"user_id": "u1"},
                workspace_path="/w",
                allow_write=True,
                timeout=10,
            )

    res = asyncio.run(run())
    assert res.exit_code == 1
    assert "not a git repository" in res.stderr


# ---------------------------------------------------------------------------
# 4. push gate (allow_write)
# ---------------------------------------------------------------------------


def test_github_push_blocked_without_write():
    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        if "test -d .git" in command:
            return {"stdout": "GIT_OK", "stderr": "", "code": 0}
        return {"stdout": "", "stderr": "", "code": 0}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            return await execute_tool(
                tool_name="github_push",
                args={"github_connection_id": "cid"},
                intent="server",
                server={"user_id": "u1"},
                workspace_path="/w",
                allow_write=False,  # blocked
                timeout=10,
            )

    res = asyncio.run(run())
    assert res.exit_code == 1
    assert "allow_write=false" in res.stderr


# ---------------------------------------------------------------------------
# 5. delegate to GitHubService (mocked transport)
# ---------------------------------------------------------------------------


def test_github_pull_delegates_and_succeeds():
    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        if "test -d .git" in command:
            return {"stdout": "GIT_OK", "stderr": "", "code": 0}
        return {"stdout": "", "stderr": "", "code": 0}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            # The service layer resolves the credential provider from the
            # connection row. auth_method='ssh' -> SSH transport.
            with patch(
                "services.github_service._get_connection_or_404",
                new=AsyncMock(return_value={"auth_method": "ssh", "host": "github.com"}),
            ):
                with patch(
                    "services.github_service.GitHubService._decrypt_key",
                    new=AsyncMock(return_value=("FAKE_KEY", "github.com")),
                ):
                    with patch(
                        "services.github_service.GitHubService.pull",
                        new=AsyncMock(return_value={"ok": True, "code": 0, "stderr": ""}),
                    ) as m_push:
                        res = await execute_tool(
                            tool_name="github_pull",
                            args={"github_connection_id": "cid", "strategy": "ff_only"},
                            intent="server",
                            server={"user_id": "u1"},
                            workspace_path="/w",
                            allow_write=True,
                            timeout=10,
                            user_id="u1",
                        )
                        assert m_push.called
                        return res

    res = asyncio.run(run())
    assert res.exit_code == 0
    assert "pull ok" in res.stdout


def test_github_push_delegates_and_fails_cleanly():
    async def fake_exec(*, server, workspace_path, command, timeout, **kw):
        if "test -d .git" in command:
            return {"stdout": "GIT_OK", "stderr": "", "code": 0}
        return {"stdout": "", "stderr": "", "code": 0}

    async def run():
        with patch("services.tools.exec_in_workspace", side_effect=fake_exec):
            with patch(
                "services.github_service._get_connection_or_404",
                new=AsyncMock(return_value={"auth_method": "ssh", "host": "github.com"}),
            ):
                with patch(
                    "services.github_service.GitHubService._decrypt_key",
                    new=AsyncMock(return_value=("FAKE_KEY", "github.com")),
                ):
                    with patch(
                        "services.github_service.GitHubService.push",
                        new=AsyncMock(
                            return_value={"ok": False, "code": 1, "stderr": "non-fast-forward"}
                        ),
                    ) as m_push:
                        res = await execute_tool(
                            tool_name="github_push",
                            args={"github_connection_id": "cid", "force": False},
                            intent="server",
                            server={"user_id": "u1"},
                            workspace_path="/w",
                            allow_write=True,
                            timeout=10,
                            user_id="u1",
                        )
                        assert m_push.called
                        return res

    res = asyncio.run(run())
    assert res.exit_code == 1
    assert "non-fast-forward" in res.stderr


# ---------------------------------------------------------------------------
# 6. Risk + approval mapping
# ---------------------------------------------------------------------------


def test_github_risk_levels():
    assert _assess_risk("github_push", {}) == "high"
    assert _assess_risk("github_pull", {}) == "medium"


def test_github_approval_type():
    from models.approval import ApprovalType

    assert _map_tool_to_approval_type("github_push") == ApprovalType.DEPLOYMENT
    assert _map_tool_to_approval_type("github_pull") == ApprovalType.DEPLOYMENT
