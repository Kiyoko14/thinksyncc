"""Shared pytest fixtures for the ThinkSync backend test-suite.

The executor resolves authoritative platform context via
``services.capability_service.load_workspace_context`` (Redis + Supabase + SSH).
Unit tests never stand those services up, so we provide a deterministic,
offline ``WorkspaceContext`` for every test that exercises ``run_server_execution``
without injecting its own ``workspace_context``.  This keeps the suite green in
CI / offline runners without touching production code paths.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services.capability_service import WorkspaceContext


def _offline_workspace_context(workspace_id: str = "workspace-test") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        port=8081,
        subdomain="test",
        protocol="http",
        gateway_available=False,
        ssl_enabled=False,
        runtime_type="python",
    )


@pytest.fixture(autouse=True)
def _mock_platform_context():
    """Patch the platform-context loader so execution never hits the network.

    Tests that intentionally exercise context-failure paths can disable this
    fixture locally; the default keeps the suite hermetic.
    """
    ctx = _offline_workspace_context()
    with patch(
        "services.executor.load_workspace_context",
        new=AsyncMock(return_value=ctx),
    ):
        yield


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Guarantee a live, open event loop for every test.

    Some suites call ``asyncio.run()`` (test_reliability_sprint), which creates
    a loop, runs it, and then *closes* it -- leaving no current event loop for
    later tests that drive async code with the deprecated
    ``asyncio.get_event_loop().run_until_complete(...)`` pattern
    (test_authorization_hardening). This fixture keeps an open loop available
    so otherwise-independent suites cannot break each other through global
    event-loop state. It does not close the loop afterwards (leaving it for the
    next test matches pytest's own default loop management).
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    yield
