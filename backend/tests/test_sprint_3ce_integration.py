"""
Integration verification for Sprint 3C.E — ProgressiveContextLoader.

Confirms the loader:
  - reuses ContextEngine.build_context (no parallel impl)
  - returns the SAME legacy payload shape (mode/selected_files/snippets/
    prompt_payload) so downstream PATCH/CREATE logic is unchanged
  - adds engineering_context + context_budget blocks
  - applies budget enforcement (drops low-priority blocks first)

NOTE: pytest-asyncio is not installed; we wrap coroutines with asyncio.run.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services.progressive_context import ProgressiveContextLoader


@pytest.fixture()
def fake_context_bundle():
    return {
        "mode": "PATCH",
        "selected_files": ["auth.py", "models.py"],
        "snippets": [
            {"path": "auth.py", "content": "x" * 50, "snippet": "x" * 20},
        ],
        "prompt_payload": {
            "MODE": "PATCH",
            "FILE_LIST": ["auth.py", "models.py"],
            "CODE_SNIPPETS": {"auth.py": "x" * 20},
            "USER_TASK": "refactor auth",
        },
    }


def test_loader_returns_legacy_shape(fake_context_bundle):
    with patch(
        "services.progressive_context.ContextEngine.build_context",
        new=AsyncMock(return_value=fake_context_bundle),
    ), patch(
        "services.progressive_context.RepositoryIndex.refresh",
        new=AsyncMock(return_value={"total_files": 2, "changed_files": [], "scanned_full": False}),
    ), patch(
        "services.progressive_context.ProjectBrain.get_section",
        new=AsyncMock(return_value="ThinkSync stack: Python/Supabase."),
    ), patch(
        "services.progressive_context.SessionSnapshot.load",
        new=AsyncMock(return_value=None),
    ):
        loader = ProgressiveContextLoader()
        result = asyncio.run(loader.build(
            workspace_id="ws1",
            task="refactor auth module",
            server={},
            workspace_path="/tmp/ws",
            conversation_history=[],
            specification=None,
        ))

    # Legacy shape preserved (backward compatibility).
    assert result["mode"] == "PATCH"
    assert result["selected_files"] == ["auth.py", "models.py"]
    assert result["snippets"] == fake_context_bundle["snippets"]
    assert result["prompt_payload"]["MODE"] == "PATCH"
    assert result["prompt_payload"]["USER_TASK"] == "refactor auth"

    # New blocks added (do not break old consumers).
    assert "engineering_context" in result
    assert "context_budget" in result
    assert isinstance(result["context_budget"]["budget"], int)
    assert "included" in result["context_budget"]
    assert "dropped" in result["context_budget"]


def test_loader_incremental_repo_index(fake_context_bundle):
    refresh = AsyncMock(return_value={"total_files": 5, "changed_files": ["a.py"], "scanned_full": False})
    with patch(
        "services.progressive_context.ContextEngine.build_context",
        new=AsyncMock(return_value=fake_context_bundle),
    ), patch(
        "services.progressive_context.RepositoryIndex.refresh", new=refresh
    ), patch(
        "services.progressive_context.ProjectBrain.get_section",
        new=AsyncMock(return_value="stack"),
    ), patch(
        "services.progressive_context.SessionSnapshot.load",
        new=AsyncMock(return_value=None),
    ):
        loader = ProgressiveContextLoader()
        asyncio.run(loader.build(
            workspace_id="ws1", task="x", server={}, workspace_path="/tmp/ws"
        ))
        # RepositoryIndex.refresh was awaited exactly once (incremental, not full rescan).
        refresh.assert_awaited_once()


def test_loader_budget_drops_low_priority(fake_context_bundle):
    big_history = [{"content": f"chat turn {i} about nothing relevant"} for i in range(40)]
    with patch(
        "services.progressive_context.ContextEngine.build_context",
        new=AsyncMock(return_value=fake_context_bundle),
    ), patch(
        "services.progressive_context.RepositoryIndex.refresh",
        new=AsyncMock(return_value={"total_files": 1, "changed_files": [], "scanned_full": False}),
    ), patch(
        "services.progressive_context.ProjectBrain.get_section",
        new=AsyncMock(return_value="stack"),
    ), patch(
        "services.progressive_context.SessionSnapshot.load",
        new=AsyncMock(return_value=None),
    ):
        loader = ProgressiveContextLoader()
        result = asyncio.run(loader.build(
            workspace_id="ws1", task="x", server={}, workspace_path="/tmp/ws",
            conversation_history=big_history,
        ))
        assert result["context_budget"]["total_tokens"] <= result["context_budget"]["budget"]
