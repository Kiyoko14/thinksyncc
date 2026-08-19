"""Tests for the duplicate-workspace policy (per the final architecture decision).

A workspace with a given name MUST NOT be created twice on the same server.
The backend returns HTTP 409 Conflict (it does NOT silently return the
existing workspace). This test exercises WorkspaceService.create_workspace
with a mocked DB that reports an existing name.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from services import workspace_service
from services.workspace_service import WorkspaceService


def _fake_supabase_with_existing(existing_row):
    """Supabase mock that reports an existing workspace with the same name.

    A SELECT (maybe_single) returns the existing row; an INSERT returns it as a
    single-element list (matching the real Supabase response shape).
    """

    class _Exec:
        def __init__(self, data):
            self.data = data

    class _Table:
        def __init__(self):
            self._last = None
            self._did_insert = False

        def insert(self, rec):
            self._last = rec
            self._did_insert = True
            return self

        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def maybe_single(self):
            return self

        async def execute(self):
            if self._did_insert:
                # Insert response shape: list containing the new row.
                return _Exec([self._last])
            # Select (by_name) found the existing workspace.
            return _Exec(existing_row)

    class _Supa:
        def table(self, _name):
            return _Table()

    return _Supa()


def test_duplicate_name_returns_409():
    existing = {
        "id": "existing-ws",
        "user_id": "u1",
        "server_id": "s1",
        "name": "thinksync",
        "path": "/w/existing",
        "slug": "thinksync-abc",
        "domain": "thinksync-abc.example.com",
        "github_connection_id": None,
        "created_at": "2026-07-17T00:00:00+00:00",
        "updated_at": "2026-07-17T00:00:00+00:00",
    }
    fake = _fake_supabase_with_existing(existing)

    async def run():
        with patch("services.workspace_service.get_supabase_async", new=AsyncMock(return_value=fake)):
            with patch(
                "services.workspace_service.ServerService.get_server",
                new=staticmethod(lambda server_id, user_id: {"id": server_id}),
            ):
                with patch(
                    "services.workspace_service.SSHService.execute",
                    new=AsyncMock(),
                ):
                    with patch(
                        "services.workspace_service.WorkspaceService.get_workspace_by_slug",
                        new=staticmethod(lambda **kw: None),
                    ):
                        return await WorkspaceService.create_workspace(
                            user_id="11111111-1111-4111-8111-111111111111",
                            server_id="22222222-2222-4222-8222-222222222222",
                            name="thinksync",
                        )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "WORKSPACE_NAME_CONFLICT"


def test_thinksync_and_github_distinct_names_still_checked():
    """The 409 policy applies uniformly to both ThinkSync and GitHub workspaces:
    a second workspace (of either type) with an existing name is rejected."""
    existing = {
        "id": "gh-ws",
        "user_id": "u1",
        "server_id": "s1",
        "name": "thinksync",
        "path": "/w/gh",
        "slug": "thinksync-xyz",
        "domain": "thinksync-xyz.example.com",
        "github_connection_id": "conn-1",  # simulates a GitHub workspace
        "created_at": "2026-07-17T00:00:00+00:00",
        "updated_at": "2026-07-17T00:00:00+00:00",
    }
    fake = _fake_supabase_with_existing(existing)

    async def run():
        with patch("services.workspace_service.get_supabase_async", new=AsyncMock(return_value=fake)):
            with patch(
                "services.workspace_service.ServerService.get_server",
                new=staticmethod(lambda server_id, user_id: {"id": server_id}),
            ):
                with patch(
                    "services.workspace_service.SSHService.execute",
                    new=AsyncMock(),
                ):
                    with patch(
                        "services.workspace_service.WorkspaceService.get_workspace_by_slug",
                        new=staticmethod(lambda **kw: None),
                    ):
                        return await WorkspaceService.create_workspace(
                            user_id="11111111-1111-4111-8111-111111111111",
                            server_id="22222222-2222-4222-8222-222222222222",
                            name="thinksync",
                            app_clone=__import__("models.github_app", fromlist=["GitHubAppCloneRequest"]).GitHubAppCloneRequest(
                                installation_id="inst-1", repo="a/thinksync"
                            ),
                        )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 409
