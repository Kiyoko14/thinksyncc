"""Provider-agnostic Git transport facade (service layer).

ARCHITECTURE RULE (authoritative):
    The agent NEVER knows how a GitHub Workspace authenticates. It only calls
    the ``github_pull`` / ``github_push`` tools with a ``github_connection_id``.
    Selecting the credential provider (SSH key vs GitHub App installation token)
    is EXCLUSIVELY the responsibility of this service layer.

Resolution:
    A workspace's linked ``github_connections`` row carries ``auth_method``:
        * ``ssh``  -> decrypt the stored key, transport via GitHubService (SSH).
        * ``app``  -> mint a short-lived installation token, transport via
                      GitHubAppService (HTTPS, token in GIT_ASKPASS env only).
    A ThinkSync (plain) workspace has NO connection -> github_pull/push error
    out with a clear, provider-neutral message.

This module is the single dispatch point. Both ``github_pull`` and
``github_push`` tools delegate here, so provider knowledge lives in ONE place.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException, status

# NOTE: github_service / github_app_service are imported lazily inside functions
# to avoid import cycles (workspace_service <-> github_* <-> git_transport).


class GitProviderError(HTTPException):
    """Provider-neutral error surfaced to the agent tool layer."""


async def _resolve_connection_row(*, connection_id: str, user_id: str) -> dict[str, Any]:
    """Fetch the owned github_connections row (raises 404 if not found).

    Reuses GitHubService's owned-row lookup so ownership + RLS semantics are
    identical to every other connection read.
    """
    from services.github_service import _get_connection_or_404

    return await _get_connection_or_404(connection_id, user_id or "")


def _provider_of(row: dict[str, Any]) -> str:
    """Return the normalized auth provider for a connection row."""
    return (row.get("auth_method") or "ssh").strip().lower()


async def workspace_pull(
    *,
    connection_id: str,
    user_id: Optional[str],
    workspace_path: str,
    server: Optional[dict[str, Any]],
    remote: str = "origin",
    branch: Optional[str] = None,
    strategy: str = "ff_only",
) -> dict[str, Any]:
    """Pull latest changes into an existing workspace repo.

    Resolves the credential provider from the connection row and dispatches to
    the matching transport. Returns a unified ``{ok, code, stderr}`` dict.
    """
    row = await _resolve_connection_row(connection_id=connection_id, user_id=user_id or "")
    provider = _provider_of(row)

    if provider == "ssh":
        from services.github_service import GitHubService

        private_key, host = await GitHubService._decrypt_key(
            connection_id=connection_id, user_id=user_id or ""
        )
        if strategy not in ("ff_only", "merge", "rebase"):
            return {"ok": False, "code": 1, "stderr": f"invalid strategy {strategy!r}"}
        return await GitHubService.pull(
            private_key_text=private_key,
            host=host,
            workspace_path=workspace_path,
            remote=remote,
            branch=branch,
            strategy=strategy,
        )

    if provider == "app":
        from services.github_app_service import GitHubAppService

        installation_id = row.get("installation_id")
        if not installation_id:
            return {
                "ok": False,
                "code": 1,
                "stderr": "app connection is missing installation_id",
            }
        return await GitHubAppService.pull(
            installation_id=str(installation_id),
            workspace_path=workspace_path,
            branch=branch,
            server=server,
        )

    return {"ok": False, "code": 1, "stderr": f"unsupported auth provider {provider!r}"}


async def workspace_push(
    *,
    connection_id: str,
    user_id: Optional[str],
    workspace_path: str,
    server: Optional[dict[str, Any]],
    remote: str = "origin",
    branch: Optional[str] = None,
    force: bool = False,
    tags: bool = False,
) -> dict[str, Any]:
    """Push commits from an existing workspace repo.

    Provider selection is identical to :func:`workspace_pull`. Push is gated by
    approval upstream (executor/agent_service); this facade only transports.
    Returns a unified ``{ok, code, stderr}`` dict.
    """
    row = await _resolve_connection_row(connection_id=connection_id, user_id=user_id or "")
    provider = _provider_of(row)

    if provider == "ssh":
        from services.github_service import GitHubService

        private_key, host = await GitHubService._decrypt_key(
            connection_id=connection_id, user_id=user_id or ""
        )
        return await GitHubService.push(
            private_key_text=private_key,
            host=host,
            workspace_path=workspace_path,
            remote=remote,
            branch=branch,
            force=force,
            tags=tags,
        )

    if provider == "app":
        from services.github_app_service import GitHubAppService

        installation_id = row.get("installation_id")
        if not installation_id:
            return {
                "ok": False,
                "code": 1,
                "stderr": "app connection is missing installation_id",
            }
        return await GitHubAppService.push(
            installation_id=str(installation_id),
            workspace_path=workspace_path,
            branch=branch,
            server=server,
        )

    return {"ok": False, "code": 1, "stderr": f"unsupported auth provider {provider!r}"}
