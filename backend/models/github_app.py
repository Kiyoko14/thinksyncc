"""GitHub App integration models (backend-only, production-ready).

This module defines the contract for the GitHub App / OAuth installation
flow. It intentionally stores NO secrets:

  * The GitHub App private key (PEM) lives only in configuration
    (``GITHUB_APP_PRIVATE_KEY``) and is used in-process to mint a short-lived
    installation token. It is NEVER written to the database.
  * Installation records (``github_app_installations``) store only metadata
    (account id/login/type, granted permissions) for audit + UX — no token,
    no PEM.

Workspace typing rule (reuses the existing model, NO migration):
  * A GitHub Workspace is any workspace whose ``github_connection_id`` is NOT
    NULL. For App-based workspaces we insert a ``github_connections`` row with
    ``auth_method='app'`` (no SSH keys) and point the workspace at it, so the
    existing frontend discriminator (``github_connection_id IS NULL`` ->
    ThinkSync) keeps working unchanged.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Installation (metadata only — no secrets)
# ---------------------------------------------------------------------------


class GitHubAppInstallation(BaseModel):
    """An installed GitHub App on a user's account/organisation."""

    id: str  # GitHub installation id
    user_id: str
    github_account_id: str
    github_account_login: str
    github_account_type: str  # "User" | "Organization"
    permissions: dict[str, Any] = Field(default_factory=dict)
    repositories_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class GitHubAppInstallationResponse(BaseModel):
    """Public shape returned to the frontend (no secrets)."""

    installation_id: str
    github_account_login: str
    github_account_type: str
    permissions: dict[str, Any] = Field(default_factory=dict)
    repositories_count: int = 0


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


class GitHubAppAuthorizeResponse(BaseModel):
    """Authorize URL the frontend redirects the browser to."""

    authorization_url: str


class GitHubAppCallbackRequest(BaseModel):
    code: str
    state: str


class GitHubAppTokenResponse(BaseModel):
    """Result of a successful callback (installation now usable)."""

    installation_id: str
    github_account_login: str
    github_account_type: str


# ---------------------------------------------------------------------------
# Repository discovery / selection
# ---------------------------------------------------------------------------


class GitHubRepository(BaseModel):
    id: int
    name: str
    full_name: str
    private: bool = False
    default_branch: Optional[str] = None
    html_url: Optional[str] = None
    permissions: dict[str, Any] = Field(default_factory=dict)


class GitHubRepositoryListResponse(BaseModel):
    installation_id: str
    repositories: list[GitHubRepository] = Field(default_factory=list)


class GitHubRepositorySelectRequest(BaseModel):
    installation_id: str
    repo_full_name: str
    branch: Optional[str] = None


# ---------------------------------------------------------------------------
# Clone request (used by WorkspaceService to select the App clone path)
# ---------------------------------------------------------------------------


class GitHubAppCloneRequest(BaseModel):
    """Tells WorkspaceService to clone via GitHub App token (not SSH)."""

    installation_id: str
    repo: str  # owner/name
    branch: Optional[str] = None
    depth: Optional[int] = None
    # Canonical (immutable) GitHub repository id — stored on the connection so
    # webhook events (repository.renamed / deleted / transfer) can map back to
    # this connection by an identifier that never changes.
    repo_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Workspace creation via GitHub App
# ---------------------------------------------------------------------------


class GitHubAppWorkspaceRequest(BaseModel):
    server_id: str
    installation_id: str
    repo_full_name: str
    branch: Optional[str] = None
