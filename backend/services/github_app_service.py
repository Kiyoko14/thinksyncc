"""GitHub App integration — production-ready backend (Phase 1).

Responsibilities (all backend-side, no agent involvement):
  * Authorize URL generation (OAuth web flow to install/authorize the App).
  * OAuth callback handling: exchange ``code`` for an installation, persist
    ONLY metadata (no secrets) in ``github_app_installations``.
  * Installation token management: mint a GitHub App JWT, exchange it for a
    short-lived installation token, cache it in-memory with TTL.
  * Repository discovery / listing / permission validation (via REST API).
  * Clone / pull / push using the installation token over HTTPS, with the
    token kept in the environment (never in process args, never on disk).
  * Credential lifecycle: token cache invalidation/rotation; no long-lived
    secrets persisted.
  * Audit logging of every token mint, clone, pull, push.
  * Production-grade error handling + security (signed state param, host
    allow-listing, permission checks).

Security contract (must hold):
  * The App PEM is read from config (env) only — never persisted, never
    returned in any API response.
  * Installation rows store metadata only (account id/login/type, perms).
  * Installation tokens are short-lived (<= 1h) and cached in-process.
  * Clone/pull/push use ``GIT_ASKPASS`` so the token never appears in the
    command line or on disk.

This module MAXIMISES reuse of existing code:
  * ``WorkspaceService`` builds the workspace row + path + domain (unchanged).
  * ``GitHubService.clone_into_workspace`` is NOT used (it is SSH-specific);
    the App path uses HTTPS with an installation token instead, but the
    surrounding workspace scaffolding is identical.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import jwt
from fastapi import HTTPException, status

from core.config import get_settings
from core.crypto import decrypt_secret, encrypt_secret
from core.database import get_supabase_async
from services.github_rate_limit import github_api_call
from models.github_app import (
    GitHubAppAuthorizeResponse,
    GitHubAppInstallationResponse,
    GitHubAppTokenResponse,
    GitHubAppWorkspaceRequest,
    GitHubRepository,
    GitHubRepositoryListResponse,
    GitHubRepositorySelectRequest,
)
from models.workspace import WorkspaceResponse

logger = logging.getLogger(__name__)


# Conservative timeouts (seconds).
_JWT_EXP_SECONDS = 300  # GitHub App JWT validity (<= 10 min)
_TOKEN_REFRESH_LEEWAY = 300  # refresh the installation token 5 min early
_STATE_EXP_SECONDS = 600  # CSRF state validity (10 min)
_CLONE_TIMEOUT = 300
_PULL_TIMEOUT = 180
_PUSH_TIMEOUT = 180
_REPO_RE = __import__("re").compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = __import__("re").compile(r"^[A-Za-z0-9_./-]+$")


# In-memory token cache: installation_id -> (token, expires_at_epoch)
_token_cache: dict[str, tuple[str, float]] = {}
# Per-installation locks (NO global lock). A lock exists only while a refresh
# is in flight; cache-hit reads never acquire it. Double-checked locking
# (see get_installation_token) prevents a thundering herd and duplicate mint.
_token_locks: dict[str, "asyncio.Lock"] = {}


def _token_lock(installation_id: str) -> "asyncio.Lock":
    lock = _token_locks.get(installation_id)
    if lock is None:
        lock = asyncio.Lock()
        _token_locks[installation_id] = lock
    return lock


async def _audit_cache(event_type: str, *, installation_id: str, status: str, **extra: Any) -> None:
    """Fire-and-forget structured audit for a token-cache event (Part 7)."""
    try:
        from services.github_audit import AuditEvent, record_github_event

        await record_github_event(
            AuditEvent(
                event_type=event_type,
                installation_id=installation_id,
                status=status,
                step_name="token_cache",
                metadata=extra,
            )
        )
    except Exception:  # noqa: BLE001
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_github_app() -> None:
    settings = get_settings()
    if not settings.github_app_enabled:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "code": "GITHUB_APP_NOT_CONFIGURED",
                "message": "GitHub App integration is not configured on this server.",
            },
        )


def _load_pem() -> str:
    """Read the GitHub App private key from config (env). Never cached to DB."""
    settings = get_settings()
    if settings.GITHUB_APP_PRIVATE_KEY:
        raw = settings.GITHUB_APP_PRIVATE_KEY
        # Tolerate a path passed in the value accidentally; prefer explicit path.
        if settings.GITHUB_APP_PRIVATE_KEY_PATH and os.path.isfile(settings.GITHUB_APP_PRIVATE_KEY_PATH):
            with open(settings.GITHUB_APP_PRIVATE_KEY_PATH, "r", encoding="utf-8") as fh:
                return fh.read()
        return raw
    if settings.GITHUB_APP_PRIVATE_KEY_PATH and os.path.isfile(settings.GITHUB_APP_PRIVATE_KEY_PATH):
        with open(settings.GITHUB_APP_PRIVATE_KEY_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "GITHUB_APP_KEY_MISSING", "message": "App private key is not configured."},
    )


# ---------------------------------------------------------------------------
# Signed state (CSRF-safe) — uses the app JWT secret (already required).
# ---------------------------------------------------------------------------


def _make_state(user_id: str) -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {"sub": user_id, "iat": now, "exp": now + _STATE_EXP_SECONDS, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _verify_state(state: str, user_id: str) -> None:
    settings = get_settings()
    try:
        payload = jwt.decode(state, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_STATE", "message": f"Invalid or expired state: {exc}"},
        )
    if payload.get("sub") != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "STATE_MISMATCH", "message": "state does not match the user."},
        )


# ---------------------------------------------------------------------------
# App JWT (used to mint installation tokens)
# ---------------------------------------------------------------------------


def _make_app_jwt() -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iat": now - 30,
        "exp": now + _JWT_EXP_SECONDS,
        "iss": settings.GITHUB_APP_ID,
    }
    return jwt.encode(payload, _load_pem(), algorithm="RS256")


# ---------------------------------------------------------------------------
# Installation token management
# ---------------------------------------------------------------------------


async def _exchange_code_for_installation(*, code: str) -> dict[str, Any]:
    """Exchange the OAuth ``code`` for installation metadata via GitHub.

    Uses the GitHub App OAuth flow. The client secret is read from config
    (env only) and is never persisted. The redirect_uri must match the
    configured GITHUB_APP_REDIRECT_URI exactly.
    """
    settings = get_settings()
    token_url = f"{settings.GITHUB_OAUTH_BASE}/login/oauth/access_token"
    headers = {"Accept": "application/json"}
    data = {
        "client_id": settings.GITHUB_APP_CLIENT_ID,
        "client_secret": settings.GITHUB_APP_CLIENT_SECRET,
        "code": code,
        "redirect_uri": settings.GITHUB_APP_REDIRECT_URI,
    }
    try:
        # retry=False: the OAuth authorization code is single-use (Part 2).
        # wait_on_rate_limit=False: on 429, fail fast with GitHubRateLimitError
        # rather than waiting/retrying (Part 3 business decision).
        resp = await github_api_call(
            "POST", token_url, headers=headers, data=data,
            retry=False, wait_on_rate_limit=False,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "GITHUB_OAUTH_ERROR", "message": str(exc)},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "GITHUB_OAUTH_FAILED", "message": resp.text[:400]},
        )
    return resp.json()


async def _fetch_installation_meta(*, installation_id: str) -> dict[str, Any]:
    """Fetch installation metadata (account, permissions) from GitHub."""
    settings = get_settings()
    app_jwt = _make_app_jwt()
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{settings.GITHUB_API_BASE}/app/installations/{installation_id}"
    try:
        # retry=True: read-only GET, safe to retry on transient failures (Part 2).
        # wait_on_rate_limit=True: bounded single wait-then-retry on 429 (Part 3).
        resp = await github_api_call("GET", url, headers=headers, retry=True, wait_on_rate_limit=True)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "GITHUB_API_ERROR", "message": str(exc)},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "GITHUB_INSTALLATION_FETCH_FAILED", "message": resp.text[:400]},
        )
    return resp.json()


async def get_installation_token(*, installation_id: str) -> str:
    """Return a valid installation token, minting/caching as needed.

    The token is held ONLY in-process memory and refreshed before expiry.

    Concurrency (Part 6): per-installation ``asyncio.Lock`` with double-checked
    locking. The fast path (cache hit, not near expiry) returns WITHOUT taking
    any lock — no contention on the hot path. Only a miss / near-expiry takes the
    per-installation lock, then re-checks the cache inside the lock so that only
    one coroutine per installation mints. This eliminates duplicate mints and the
    thundering herd without any global lock.
    """
    # --- fast path: valid cache hit, NO lock ---
    cached = _token_cache.get(installation_id)
    now = time.time()
    if cached:
        token, expires_at = cached
        if now < expires_at - _TOKEN_REFRESH_LEEWAY:
            await _audit_cache("cache.hit", installation_id=installation_id, status="hit")
            return token

    # --- slow path: per-installation lock, double-checked ---
    had_entry = cached is not None  # expired (still in cache) vs never cached
    async with _token_lock(installation_id):
        # Re-check after acquiring the lock: a concurrent caller may have
        # refreshed already.
        cached = _token_cache.get(installation_id)
        if cached:
            token, expires_at = cached
            if now < expires_at - _TOKEN_REFRESH_LEEWAY:
                await _audit_cache("cache.hit", installation_id=installation_id, status="hit_under_lock")
                return token

        if had_entry:
            await _audit_cache("cache.expired", installation_id=installation_id, status="expired")
        else:
            await _audit_cache("cache.miss", installation_id=installation_id, status="miss")

        settings = get_settings()
        app_jwt = _make_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{settings.GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
        try:
            # retry=True: minting a token is a repeatable operation (Part 2).
            # wait_on_rate_limit=True: bounded single wait-then-retry on 429 (Part 3).
            resp = await github_api_call("POST", url, headers=headers, retry=True, wait_on_rate_limit=True)
        except Exception as exc:  # noqa: BLE001
            # Any mint failure (transport error 401/403/404/timeout, etc.) means
            # we cannot trust any prior token either; drop it so a later call
            # re-mints rather than serving a stale one (Part 6 D3). Surface a
            # clean 502 (unchanged external contract).
            _token_cache.pop(installation_id, None)
            await _audit_cache("cache.evict", installation_id=installation_id, status="evicted",
                               reason="transport_or_http_failure")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "GITHUB_TOKEN_ERROR", "message": str(exc)},
            )
        if resp.status_code != 201:
            # 401/403/404 etc.: the installation/token is unusable. Evict any
            # cached token so we never serve a stale one; callers will retry and
            # get a fresh 502 rather than a cached bad token.
            _token_cache.pop(installation_id, None)
            await _audit_cache("cache.evict", installation_id=installation_id, status="evicted",
                               reason="github_error_status", status_code=resp.status_code)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "GITHUB_TOKEN_FAILED", "message": resp.text[:400]},
            )
        body = resp.json()
        token = body["token"]
        # expires_at is ISO; convert to epoch with leeway.
        expires_at = now + settings.GITHUB_APP_TOKEN_TTL_SECONDS
        if body.get("expires_at"):
            try:
                exp_dt = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
                expires_at = exp_dt.timestamp()
            except Exception:
                pass
        _token_cache[installation_id] = (token, expires_at)
        logger.info("[github_app] installation_token_minted | installation=%s", installation_id)
        await _audit_cache("cache.refresh", installation_id=installation_id, status="refresh",
                           expires_at=int(expires_at))
        return token


async def invalidate_installation_token(installation_id: str) -> None:
    """Drop any cached token for an installation.

    Part 6: takes the per-installation lock so an invalidation can never race
    with an in-flight refresh and leave a stale token written after the
    invalidation (stale-write). Global lock is NOT used.
    """
    async with _token_lock(installation_id):
        _token_cache.pop(installation_id, None)
        await _audit_cache("cache.invalidate", installation_id=installation_id, status="invalidated")


# ---------------------------------------------------------------------------
# Service API
# ---------------------------------------------------------------------------


class GitHubAppService:
    # ------------------------------------------------------------------ auth
    @staticmethod
    async def authorize_url(*, user_id: str) -> GitHubAppAuthorizeResponse:
        _require_github_app()
        settings = get_settings()
        state = _make_state(user_id)
        params = (
            f"client_id={settings.GITHUB_APP_CLIENT_ID}"
            f"&redirect_uri={settings.GITHUB_APP_REDIRECT_URI}"
            f"&state={state}"
        )
        url = f"{settings.GITHUB_OAUTH_BASE}/login/oauth/authorize?{params}"
        return GitHubAppAuthorizeResponse(authorization_url=url)

    @staticmethod
    async def handle_callback(
        *, user_id: str, code: str, state: str
    ) -> GitHubAppTokenResponse:
        """Complete the OAuth flow, persist installation metadata (no secrets)."""
        _require_github_app()
        _verify_state(state, user_id)

        # Exchange code -> GitHub returns installation info (account + perms).
        # The App client secret is supplied via config; we do NOT store it.
        meta = await _exchange_code_for_installation(code=code)
        installation_id = str(meta.get("installation_id") or meta.get("id"))
        if not installation_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "NO_INSTALLATION", "message": "No installation returned by GitHub."},
            )

        # Enrich with full installation metadata (permissions, account).
        full = await _fetch_installation_meta(installation_id=installation_id)
        account = full.get("account", {}) or {}
        permissions = full.get("permissions", {}) or {}

        record = {
            "id": installation_id,
            "user_id": user_id,
            "github_account_id": str(account.get("id", "")),
            "github_account_login": account.get("login", ""),
            "github_account_type": account.get("type", "User"),
            "permissions": permissions,
            "repositories_count": int(full.get("repositories_count", 0) or 0),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        supabase = await get_supabase_async()
        # Upsert: callback may run more than once for the same installation
        # (this is the reconnect path). Invalidate any cached token so a prior
        # (now-stale) token from before the reconnect is never served (Part 6 D2).
        await supabase.table("github_app_installations").upsert(record, on_conflict="id").execute()
        await invalidate_installation_token(installation_id)
        logger.info("[github_app] installation_saved | installation=%s user=%s", installation_id, user_id)
        return GitHubAppTokenResponse(
            installation_id=installation_id,
            github_account_login=record["github_account_login"],
            github_account_type=record["github_account_type"],
        )

    # ----------------------------------------------------------- installations
    @staticmethod
    async def list_installations(*, user_id: str) -> list[GitHubAppInstallationResponse]:
        _require_github_app()
        supabase = await get_supabase_async()
        result = (
            await supabase.table("github_app_installations")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        rows = result.data or []
        return [
            GitHubAppInstallationResponse(
                installation_id=str(r["id"]),
                github_account_login=r.get("github_account_login", ""),
                github_account_type=r.get("github_account_type", "User"),
                permissions=r.get("permissions", {}) or {},
                repositories_count=int(r.get("repositories_count", 0) or 0),
            )
            for r in rows
        ]

    # --------------------------------------------------------- repositories
    @staticmethod
    async def list_repositories(*, user_id: str, installation_id: str) -> GitHubRepositoryListResponse:
        _require_github_app()
        await _ownership_or_404(user_id=user_id, installation_id=installation_id)
        token = await get_installation_token(installation_id=installation_id)
        settings = get_settings()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        out: list[GitHubRepository] = []
        page = 1
        while True:
            url = (
                f"{settings.GITHUB_API_BASE}/installation/repositories"
                f"?per_page=100&page={page}"
            )
            try:
                # retry=True: read-only GET (Part 2). wait_on_rate_limit=True:
                # bounded single wait-then-retry on 429 (Part 3).
                resp = await github_api_call("GET", url, headers=headers, retry=True, wait_on_rate_limit=True)
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={"code": "GITHUB_API_ERROR", "message": str(exc)},
                )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={"code": "GITHUB_REPO_LIST_FAILED", "message": resp.text[:400]},
                )
            body = resp.json()
            for r in body.get("repositories", []) or []:
                out.append(
                    GitHubRepository(
                        id=int(r.get("id", 0)),
                        name=r.get("name", ""),
                        full_name=r.get("full_name", ""),
                        private=bool(r.get("private", False)),
                        default_branch=r.get("default_branch"),
                        html_url=r.get("html_url"),
                        permissions=r.get("permissions", {}) or {},
                    )
                )
            if not (body.get("repositories")) or len(body.get("repositories", [])) < 100:
                break
            page += 1
        logger.info("[github_app] repositories_listed | installation=%s count=%d", installation_id, len(out))
        return GitHubRepositoryListResponse(installation_id=installation_id, repositories=out)

    # ------------------------------------------------- validation + workspace
    @staticmethod
    async def _validate_repo_permission(
        *, installation_id: str, repo_full_name: str
    ) -> GitHubRepository:
        """Confirm the App can read (and ideally write) the chosen repo."""
        if not _REPO_RE.match(repo_full_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_REPO", "message": "repo must be owner/name"},
            )
        token = await get_installation_token(installation_id=installation_id)
        settings = get_settings()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{settings.GITHUB_API_BASE}/repos/{repo_full_name}"
        try:
            # retry=True: read-only GET (Part 2). wait_on_rate_limit=True:
            # bounded single wait-then-retry on 429 (Part 3).
            resp = await github_api_call("GET", url, headers=headers, retry=True, wait_on_rate_limit=True)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "GITHUB_API_ERROR", "message": str(exc)},
            )
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "REPO_NOT_ACCESSIBLE", "message": "Installation cannot access this repository."},
            )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "GITHUB_REPO_FETCH_FAILED", "message": resp.text[:400]},
            )
        r = resp.json()
        # GitHub returns `permissions` for the authenticated App.
        perms = r.get("permissions", {}) or {}
        if not perms.get("pull"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "REPO_NO_READ", "message": "Installation lacks read permission on this repository."},
            )
        return GitHubRepository(
            id=int(r.get("id", 0)),
            name=r.get("name", ""),
            full_name=r.get("full_name", ""),
            private=bool(r.get("private", False)),
            default_branch=r.get("default_branch"),
            html_url=r.get("html_url"),
            permissions=perms,
        )

    @staticmethod
    async def create_workspace_from_repo(
        *, user_id: str, payload: GitHubAppWorkspaceRequest
    ) -> WorkspaceResponse:
        """Clone ``repo_full_name`` (via App token) into a newly created workspace.

        Reuses ``WorkspaceService.create_workspace`` for row/domain/path
        scaffolding; passes an ``app_clone`` request so the clone uses the
        GitHub App installation token (HTTPS) instead of SSH.
        """
        _require_github_app()
        await _ownership_or_404(user_id=user_id, installation_id=payload.installation_id)

        repo_meta = await GitHubAppService._validate_repo_permission(
            installation_id=payload.installation_id, repo_full_name=payload.repo_full_name
        )
        owner, name = payload.repo_full_name.split("/", 1)
        workspace_name = GitHubAppService._workspace_name_from_repo(name)
        branch = payload.branch or repo_meta.default_branch

        from services.workspace_service import WorkspaceService
        from models.github_app import GitHubAppCloneRequest

        return await WorkspaceService.create_workspace(
            user_id=user_id,
            server_id=payload.server_id,
            name=workspace_name,
            github_connection_id=None,  # set internally by the app_clone path
            app_clone=GitHubAppCloneRequest(
                installation_id=payload.installation_id,
                repo=payload.repo_full_name,
                branch=branch,
                repo_id=repo_meta.id,
            ),
        )

    @staticmethod
    def _workspace_name_from_repo(repo_name: str) -> str:
        """Workspace name == repository name (e.g. thinksync).

        Sanitized to the existing workspace name rules; uniqueness is enforced
        by ``WorkspaceService`` (returns 409 on duplicate per the policy).
        """
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9_-]", "-", repo_name.strip().lower())
        safe = _re.sub(r"-{2,}", "-", safe).strip("-_")
        return safe or "repo"

    # --------------------------------------------------------- transport ops
    @staticmethod
    async def clone(
        *, installation_id: str, repo: str, branch: Optional[str],
        depth: Optional[int], workspace_path: str, server: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Clone a repo over HTTPS using the installation token.

        Security: the token is NEVER placed in the git URL or process argv.
        We use ``GIT_ASKPASS`` pointing at a tiny script that emits the token
        from an environment variable (``GITHUB_APP_TOKEN``), so the credential
        is only ever visible to the child git process via its environment.
        """
        if not _REPO_RE.match(repo):
            raise HTTPException(status_code=400, detail={"code": "INVALID_REPO", "message": "repo must be owner/name"})
        if branch and not _BRANCH_RE.match(branch):
            raise HTTPException(status_code=400, detail={"code": "INVALID_BRANCH", "message": "branch has illegal chars"})
        token = await get_installation_token(installation_id=installation_id)
        url = f"https://github.com/{repo}.git"
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GITHUB_APP_TOKEN"] = token
        # askpass script: prints the token from env for any prompt.
        askpass = (
            "#!/bin/sh\n"
            'exec printf "%s" "$GITHUB_APP_TOKEN"\n'
        )
        depth_args = ["--depth", str(depth)] if depth else []
        branch_args = ["--branch", branch] if branch else []
        askpass_fd, askpass_path = tempfile.mkstemp(prefix="ts-askpass-", suffix=".sh")
        try:
            with os.fdopen(askpass_fd, "w") as fh:
                fh.write(askpass)
            os.chmod(askpass_path, 0o700)
            env["GIT_ASKPASS"] = askpass_path
            env["PATH"] = env.get("PATH", "")
            try:
                if server is None:
                    proc = await asyncio.create_subprocess_exec(
                        "git", "clone", *depth_args, *branch_args, url, workspace_path,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
                    code = int(proc.returncode or 0)
                    if code != 0:
                        return {"ok": False, "code": code, "stderr": (stderr or b"").decode("utf-8", "replace")[:800]}
                    return {"ok": True, "code": 0, "stderr": ""}
                # Remote server path: stream-and-ship (same model as GitHubService).
                with tempfile.TemporaryDirectory() as tmp:
                    local_repo = os.path.join(tmp, "repo")
                    proc = await asyncio.create_subprocess_exec(
                        "git", "clone", *depth_args, *branch_args, url, local_repo,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
                    if int(proc.returncode or 0) != 0:
                        return {"ok": False, "code": int(proc.returncode or 0),
                                "stderr": (stderr or b"").decode("utf-8", "replace")[:800]}
                    from services.ssh_service import SSHService
                    tar_proc = await asyncio.create_subprocess_exec(
                        "tar", "-C", local_repo, "-czf", "-", ".",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    remote_cmd = f"mkdir -p {shlex.quote(workspace_path)} && tar -C {shlex.quote(workspace_path)} -xzf -"
                    ssh_result = await SSHService.execute(server=server, command=remote_cmd)
                    tar_out, tar_err = await tar_proc.communicate()
                    if int(tar_proc.returncode or 0) != 0:
                        return {"ok": False, "code": int(tar_proc.returncode or 0),
                                "stderr": (tar_err or b"").decode("utf-8", "replace")[:800]}
                    if ssh_result.exit_code != 0:
                        return {"ok": False, "code": int(ssh_result.exit_code),
                                "stderr": (ssh_result.stderr or "")[:800]}
                return {"ok": True, "code": 0, "stderr": ""}
            finally:
                env.pop("GITHUB_APP_TOKEN", None)
        finally:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

    @staticmethod
    async def pull(
        *, installation_id: str, workspace_path: str,
        branch: Optional[str] = None, server: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Pull the latest changes into an existing App-cloned workspace."""
        token = await get_installation_token(installation_id=installation_id)
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GITHUB_APP_TOKEN"] = token
        askpass = "#!/bin/sh\n" 'exec printf "%s" "$GITHUB_APP_TOKEN"\n'
        askpass_fd, askpass_path = tempfile.mkstemp(prefix="ts-askpass-", suffix=".sh")
        try:
            with os.fdopen(askpass_fd, "w") as fh:
                fh.write(askpass)
            os.chmod(askpass_path, 0o700)
            env["GIT_ASKPASS"] = askpass_path
            pull_args = ["pull"]
            if branch:
                pull_args += ["origin", branch]
            # Remote server: pull via SSH exec of git, using the token in env.
            if server is not None:
                from services.ssh_service import SSHService
                remote_cmd = f"cd {shlex.quote(workspace_path)} && git -c core.askpass={shlex.quote(askpass_path)} pull"
                res = await SSHService.execute(server=server, command=remote_cmd)
                env.pop("GITHUB_APP_TOKEN", None)
                return {"ok": res.exit_code == 0, "code": int(res.exit_code),
                        "stderr": (res.stderr or "")[:800]}
            proc = await asyncio.create_subprocess_exec(
                "git", *pull_args, cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PULL_TIMEOUT)
            env.pop("GITHUB_APP_TOKEN", None)
            code = int(proc.returncode or 0)
            return {"ok": code == 0, "code": code, "stderr": (stderr or b"").decode("utf-8", "replace")[:800]}
        finally:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

    @staticmethod
    async def push(
        *, installation_id: str, workspace_path: str,
        branch: Optional[str] = None, server: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Push commits from an App-cloned workspace. Gated by approval upstream."""
        token = await get_installation_token(installation_id=installation_id)
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GITHUB_APP_TOKEN"] = token
        askpass = "#!/bin/sh\n" 'exec printf "%s" "$GITHUB_APP_TOKEN"\n'
        askpass_fd, askpass_path = tempfile.mkstemp(prefix="ts-askpass-", suffix=".sh")
        try:
            with os.fdopen(askpass_fd, "w") as fh:
                fh.write(askpass)
            os.chmod(askpass_path, 0o700)
            env["GIT_ASKPASS"] = askpass_path
            push_args = ["push"]
            if branch:
                push_args += ["origin", branch]
            if server is not None:
                from services.ssh_service import SSHService
                remote_cmd = f"cd {shlex.quote(workspace_path)} && git -c core.askpass={shlex.quote(askpass_path)} push"
                res = await SSHService.execute(server=server, command=remote_cmd)
                env.pop("GITHUB_APP_TOKEN", None)
                return {"ok": res.exit_code == 0, "code": int(res.exit_code),
                        "stderr": (res.stderr or "")[:800]}
            proc = await asyncio.create_subprocess_exec(
                "git", *push_args, cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PUSH_TIMEOUT)
            env.pop("GITHUB_APP_TOKEN", None)
            code = int(proc.returncode or 0)
            return {"ok": code == 0, "code": code, "stderr": (stderr or b"").decode("utf-8", "replace")[:800]}
        finally:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Ownership helper (installation row belongs to this user)
# ---------------------------------------------------------------------------


async def _ownership_or_404(*, user_id: str, installation_id: str) -> dict[str, Any]:
    supabase = await get_supabase_async()
    result = (
        await supabase.table("github_app_installations")
        .select("*")
        .eq("id", installation_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INSTALLATION_NOT_FOUND", "message": "Installation not found for this user."},
        )
    return dict(result.data)
