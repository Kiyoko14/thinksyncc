"""Tests for the provider-agnostic Git transport facade (git_transport).

ARCHITECTURE RULE under test:
    The agent calls github_pull / github_push with only a github_connection_id.
    The SERVICE LAYER (git_transport) selects the credential provider from the
    connection row's auth_method:
        * 'ssh' -> GitHubService (decrypt key, SSH transport)
        * 'app' -> GitHubAppService (installation token, HTTPS transport)
        * anything else -> provider-neutral error
    The agent never sees SSH / OAuth / App details.

No network: transports are mocked. We assert the DISPATCH, not git itself.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from services import git_transport


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. SSH connection -> SSH transport (pull)
# ---------------------------------------------------------------------------


def test_pull_ssh_provider_routes_to_github_service():
    async def run():
        with patch(
            "services.github_service._get_connection_or_404",
            new=AsyncMock(return_value={"auth_method": "ssh", "host": "github.com"}),
        ):
            with patch(
                "services.github_service.GitHubService._decrypt_key",
                new=AsyncMock(return_value=("KEY", "github.com")),
            ):
                with patch(
                    "services.github_service.GitHubService.pull",
                    new=AsyncMock(return_value={"ok": True, "code": 0, "stderr": ""}),
                ) as m_ssh_pull:
                    with patch(
                        "services.github_app_service.GitHubAppService.pull",
                        new=AsyncMock(),
                    ) as m_app_pull:
                        res = await git_transport.workspace_pull(
                            connection_id="cid",
                            user_id="u1",
                            workspace_path="/w",
                            server=None,
                            strategy="ff_only",
                        )
                        assert m_ssh_pull.called
                        assert not m_app_pull.called
                        return res

    res = _run(run())
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# 2. App connection -> App transport (pull), installation_id from the column
# ---------------------------------------------------------------------------


def test_pull_app_provider_routes_to_github_app_service():
    async def run():
        with patch(
            "services.github_service._get_connection_or_404",
            new=AsyncMock(
                return_value={"auth_method": "app", "installation_id": "inst-42"}
            ),
        ):
            with patch(
                "services.github_service.GitHubService.pull",
                new=AsyncMock(),
            ) as m_ssh_pull:
                with patch(
                    "services.github_app_service.GitHubAppService.pull",
                    new=AsyncMock(return_value={"ok": True, "code": 0, "stderr": ""}),
                ) as m_app_pull:
                    res = await git_transport.workspace_pull(
                        connection_id="cid",
                        user_id="u1",
                        workspace_path="/w",
                        server=None,
                    )
                    # App transport called with the installation id from the column.
                    assert m_app_pull.called
                    _, kwargs = m_app_pull.call_args
                    assert kwargs["installation_id"] == "inst-42"
                    assert not m_ssh_pull.called
                    return res

    res = _run(run())
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# 3. App connection -> App transport (push)
# ---------------------------------------------------------------------------


def test_push_app_provider_routes_to_github_app_service():
    async def run():
        with patch(
            "services.github_service._get_connection_or_404",
            new=AsyncMock(
                return_value={"auth_method": "app", "installation_id": "inst-7"}
            ),
        ):
            with patch(
                "services.github_app_service.GitHubAppService.push",
                new=AsyncMock(return_value={"ok": True, "code": 0, "stderr": ""}),
            ) as m_app_push:
                res = await git_transport.workspace_push(
                    connection_id="cid",
                    user_id="u1",
                    workspace_path="/w",
                    server=None,
                    force=False,
                )
                assert m_app_push.called
                _, kwargs = m_app_push.call_args
                assert kwargs["installation_id"] == "inst-7"
                return res

    res = _run(run())
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# 4. App connection missing installation_id -> clean error (no crash)
# ---------------------------------------------------------------------------


def test_app_provider_without_installation_id_errors():
    async def run():
        with patch(
            "services.github_service._get_connection_or_404",
            new=AsyncMock(return_value={"auth_method": "app", "installation_id": None}),
        ):
            return await git_transport.workspace_pull(
                connection_id="cid",
                user_id="u1",
                workspace_path="/w",
                server=None,
            )

    res = _run(run())
    assert res["ok"] is False
    assert "installation_id" in res["stderr"]


# ---------------------------------------------------------------------------
# 5. Unknown provider -> provider-neutral error
# ---------------------------------------------------------------------------


def test_unknown_provider_errors():
    async def run():
        with patch(
            "services.github_service._get_connection_or_404",
            new=AsyncMock(return_value={"auth_method": "smtp"}),
        ):
            return await git_transport.workspace_push(
                connection_id="cid",
                user_id="u1",
                workspace_path="/w",
                server=None,
            )

    res = _run(run())
    assert res["ok"] is False
    assert "unsupported auth provider" in res["stderr"]
