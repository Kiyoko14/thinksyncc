"""GitHub Connection & credential management (backend-only).

This module owns EVERYTHING in the GitHub integration that the *agent* must
NOT do:

  * Storing GitHub SSH credentials (encrypted at rest via core.crypto).
  * Generating SSH keypairs on the backend (for the user to add to GitHub).
  * Validating a key can actually authenticate to a repository.
  * Resolving repository metadata (owner / name / default branch) over SSH.
  * Cloning a repository into a workspace (done by the backend at workspace
    creation time, never by the agent).
  * Pull / push over an existing workspace repo (tools call into here).

Agent-facing GitHub *tools* (github_pull / github_push) live in
services/tools.py and delegate the actual git transport to the helpers in
this module.  The agent never sees a private key and never runs `git clone`.

Security contract (must hold):
  * Private keys are encrypted at rest (``enc:v1:`` prefix) and are never
    serialized into any API response.
  * Decryption happens in-process, the key is passed to git only via an
    in-memory ``GIT_SSH_COMMAND`` environment (no temp key file written to
    disk unless strictly necessary, and then cleaned up immediately).
  * ``github_push`` is gated by ``agent_service._assess_risk == 'high'``
    AND an explicit human approval step (enforced in the executor/orchestrator).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, status

from core.crypto import decrypt_secret, encrypt_secret
from core.database import get_supabase, get_supabase_async
from models.github import (
    GitHubAuthMethod,
    GitHubCloneRequest,
    GitHubConnectionCreate,
    GitHubConnectionResponse,
    GitHubConnectionWithKey,
    GitHubRepoAccessRequest,
    GitHubRepoAccessResponse,
    GitHubRepoMetadata,
)

logger = logging.getLogger(__name__)


_GITHUB_SSH_PORT = 22
# Conservative command timeouts (seconds).
_KEYGEN_TIMEOUT = 30
_VALIDATE_TIMEOUT = 30
_META_TIMEOUT = 30
_CLONE_TIMEOUT = 300
_PULL_TIMEOUT = 180
_PUSH_TIMEOUT = 180

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


# ===========================================================================
# Helpers — key handling
# ===========================================================================


def _detect_key_type(public_key: str) -> str:
    """Best-effort key-type detection from a public key string."""
    if not public_key:
        return "unknown"
    if public_key.startswith("ssh-ed25519"):
        return "ed25519"
    if public_key.startswith("ssh-rsa"):
        return "rsa"
    if public_key.startswith("ecdsa-"):
        return "ecdsa"
    if public_key.startswith("sk-") or "sk-ssh" in public_key:
        return "fido"
    return "unknown"


def _require_nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_FIELD", "message": f"{field} is required"},
        )
    return value.strip()


def _validate_repo_slug(repo: str) -> str:
    if not _REPO_RE.match(repo or ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_REPO", "message": "repo must be 'owner/name'"},
        )
    return repo.strip()


def _ssh_url(host: str, repo: str) -> str:
    return f"git@{host}:{repo}.git"


# ===========================================================================
# DB helpers
# ===========================================================================


def _conn_row_to_response(row: dict[str, Any]) -> GitHubConnectionResponse:
    """Map a DB row to the public response (private key is dropped)."""
    return GitHubConnectionResponse(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=row["name"],
        auth_method=row.get("auth_method") or "ssh",
        host=row.get("host") or "github.com",
        ssh_public_key=row.get("ssh_public_key"),
        ssh_key_type=row.get("ssh_key_type"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _get_connection_or_404(connection_id: str, user_id: str) -> dict[str, Any]:
    supabase = await get_supabase_async()
    result = (
        await supabase.table("github_connections")
        .select("*")
        .eq("id", connection_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "GITHUB_CONNECTION_NOT_FOUND"},
        )
    return dict(result.data)


# ===========================================================================
# Key generation
# ===========================================================================


async def generate_keypair(*, connection_id: str) -> tuple[str, str]:
    """Generate an ed25519 keypair on the backend.

    Returns ``(private_key_pem, public_key)``. The private key is returned
    to the caller EXACTLY ONCE (so they can add it to GitHub). ThinkSync
    encrypts and stores it, but never returns it again.
    """
    key_id = str(connection_id)[:8] or "thinksync"
    with tempfile.TemporaryDirectory() as tmp:
        priv_path = os.path.join(tmp, f"id_{key_id}")
        pub_path = f"{priv_path}.pub"
        proc = await asyncio.create_subprocess_exec(
            "ssh-keygen",
            "-t", "ed25519",
            "-N", "",                       # no passphrase
            "-C", f"thinksync-{key_id}",
            "-f", priv_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_KEYGEN_TIMEOUT)
        if proc.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "KEYGEN_FAILED", "message": (stderr or b"").decode("utf-8", "replace")},
            )
        try:
            with open(priv_path, "r", encoding="utf-8") as fh:
                private_key = fh.read()
            with open(pub_path, "r", encoding="utf-8") as fh:
                public_key = fh.read().strip()
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "KEYGEN_READ_FAILED", "message": str(exc)},
            )
    return private_key, public_key


# ===========================================================================
# Key validation (does the credential actually authenticate?)
# ===========================================================================


def _build_ssh_command(private_key_text: str, host: str) -> str:
    """Build a ``GIT_SSH_COMMAND`` that uses the given key and disables
    host-key prompting (GitHub's host key is well-known; we still record
    the server's key via ``StrictHostKeyChecking=accept-new`` so a MITM is
    detectable on subsequent connections)."""
    with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as tf:
        tf.write(private_key_text)
        tf.flush()
        os.chmod(tf.name, 0o600)
        key_path = tf.name
    # Return both the command and the temp path so the caller can clean up.
    cmd = (
        f"ssh -i {shlex.quote(key_path)} "
        f"-o StrictHostKeyChecking=accept-new "
        f"-o IdentitiesOnly=yes "
        f"-o BatchMode=yes "
        f"-o ConnectTimeout=15 "
        f"-p {_GITHUB_SSH_PORT} "
        f"-F /dev/null"
    )
    return cmd, key_path


async def _run_git_ssh(
    *,
    private_key_text: str,
    host: str,
    repo: str,
    args: list[str],
    timeout: int,
    cwd: Optional[str] = None,
) -> dict[str, Any]:
    """Run a git command over SSH using an in-memory key (no persisted key file
    beyond a short-lived temp file that is removed before returning)."""
    ssh_cmd, key_path = _build_ssh_command(private_key_text, host)
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = ssh_cmd
    env["GIT_TERMINAL_PROMPT"] = "0"
    url = _ssh_url(host, repo)
    command = ["git", *args, url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"code": 124, "stdout": "", "stderr": f"git {args[0]} timed out after {timeout}s"}
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass
    return {
        "code": int(proc.returncode or 0),
        "stdout": (stdout or b"").decode("utf-8", "replace"),
        "stderr": (stderr or b"").decode("utf-8", "replace"),
    }


# ===========================================================================
# Service API
# ===========================================================================


class GitHubService:
    """Credential vault + transport backend for GitHub integration."""

    # ----------------------------------------------------------------------
    # Connection CRUD
    # ----------------------------------------------------------------------

    @staticmethod
    async def create_connection(
        *, user_id: str, payload: GitHubConnectionCreate
    ) -> GitHubConnectionResponse | GitHubConnectionWithKey:
        # Normalize / validate mode.
        if payload.auth_method != GitHubAuthMethod.SSH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "UNSUPPORTED_AUTH", "message": "Only 'ssh' auth is supported."},
            )
        host = _require_nonempty(payload.host, "host")
        name = _require_nonempty(payload.name, "name")

        connection_id = str(uuid.uuid4())
        private_key: Optional[str] = None
        public_key: Optional[str] = None

        if payload.generate_keypair:
            if payload.ssh_private_key or payload.ssh_public_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "INVALID_MODE", "message": "Do not supply keys when generate_keypair=true"},
                )
            private_key, public_key = await generate_keypair(connection_id=connection_id)
        else:
            private_key = payload.ssh_private_key
            public_key = payload.ssh_public_key
            if not private_key or not public_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "KEYS_REQUIRED", "message": "Provide both ssh_private_key and ssh_public_key, or set generate_keypair=true."},
                )
            # Validate the supplied private key is parseable (reject garbage early).
            try:
                from asyncssh import import_private_key  # local import; heavy
                import_private_key(private_key)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "INVALID_PRIVATE_KEY", "message": f"Private key could not be parsed: {exc}"},
                )

        key_type = _detect_key_type(public_key)
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": connection_id,
            "user_id": user_id,
            "name": name,
            "auth_method": "ssh",
            "host": host,
            "ssh_public_key": public_key,
            "ssh_private_key": encrypt_secret(private_key),  # ENCRYPT AT REST
            "ssh_key_type": key_type,
            "created_at": now,
            "updated_at": now,
        }

        supabase = await get_supabase_async()
        try:
            result = await supabase.table("github_connections").insert(record).execute()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[github] insert_failed | user=%s", user_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "GITHUB_CONNECTION_CREATE_FAILED", "message": str(exc)},
            )
        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "GITHUB_CONNECTION_CREATE_FAILED"},
            )

        row = dict(result.data[0])
        response = _conn_row_to_response(row)
        if payload.generate_keypair:
            # Return the private key EXACTLY ONCE.
            return GitHubConnectionWithKey(**response.model_dump(), ssh_private_key=private_key)
        return response

    @staticmethod
    async def list_connections(*, user_id: str) -> list[GitHubConnectionResponse]:
        supabase = await get_supabase_async()
        result = (
            await supabase.table("github_connections")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        rows = result.data or []
        return [_conn_row_to_response(dict(r)) for r in rows]

    @staticmethod
    async def get_connection(*, connection_id: str, user_id: str) -> GitHubConnectionResponse:
        row = await _get_connection_or_404(connection_id, user_id)
        return _conn_row_to_response(row)

    @staticmethod
    async def delete_connection(*, connection_id: str, user_id: str) -> None:
        # Ensure ownership first.
        await _get_connection_or_404(connection_id, user_id)
        supabase = await get_supabase_async()
        await supabase.table("github_connections").delete().eq("id", connection_id).eq("user_id", user_id).execute()

    # ----------------------------------------------------------------------
    # Decrypt helper (used by clone/pull/push)
    # ----------------------------------------------------------------------

    @staticmethod
    async def _decrypt_key(*, connection_id: str, user_id: str) -> tuple[str, str]:
        """Return ``(decrypted_private_key, host)`` for an owned connection."""
        row = await _get_connection_or_404(connection_id, user_id)
        encrypted = row.get("ssh_private_key")
        if not encrypted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "NO_KEY", "message": "This connection has no private key stored."},
            )
        decrypted = decrypt_secret(encrypted)
        host = row.get("host") or "github.com"
        return decrypted, host

    # ----------------------------------------------------------------------
    # Repository access check (over SSH, no clone)
    # ----------------------------------------------------------------------

    @staticmethod
    async def check_repo_access(
        *, user_id: str, connection_id: str, payload: GitHubRepoAccessRequest
    ) -> GitHubRepoAccessResponse:
        repo = _validate_repo_slug(payload.repo)
        private_key, host = await GitHubService._decrypt_key(connection_id=connection_id, user_id=user_id)
        ref = payload.ref
        if ref and not _BRANCH_RE.match(ref):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_REF", "message": "ref contains illegal characters"},
            )
        check_ref = ref or "HEAD"
        # `git ls-remote` validates both read access and ref existence.
        res = await _run_git_ssh(
            private_key_text=private_key,
            host=host,
            repo=repo,
            args=["ls-remote", "--heads", "--tags", check_ref],
            timeout=_VALIDATE_TIMEOUT,
        )
        if res["code"] != 0:
            return GitHubRepoAccessResponse(
                reachable=False,
                repo=repo,
                resolved_ref=ref,
                can_read=False,
                can_write=False,
                message=(res["stderr"] or res["stdout"]).strip()[:400] or "access check failed",
            )
        # If a specific ref was requested, can_read=True if it resolved.
        lines = [ln for ln in (res["stdout"] or "").splitlines() if ln.strip()]
        resolved = None
        if ref:
            # Find the matching ref line.
            for ln in lines:
                if ln.strip().endswith(f"refs/heads/{ref}") or ln.strip().endswith(f"refs/tags/{ref}"):
                    resolved = ref
                    break
            can_read = resolved is not None
        else:
            can_read = bool(lines)
            # default branch: try to read HEAD symref.
            head_res = await _run_git_ssh(
                private_key_text=private_key,
                host=host,
                repo=repo,
                args=["ls-remote", "--symref", "HEAD"],
                timeout=_VALIDATE_TIMEOUT,
            )
            for ln in (head_res["stdout"] or "").splitlines():
                if ln.startswith("ref:") and "refs/heads/" in ln:
                    resolved = ln.split("refs/heads/")[-1].strip()
                    break
        return GitHubRepoAccessResponse(
            reachable=True,
            repo=repo,
            resolved_ref=resolved,
            default_branch=resolved if not ref else None,
            can_read=can_read,
            # `ls-remote` cannot prove write access; push is the real test and
            # is gated by approval. Report can_write=False conservatively.
            can_write=False,
            message="read access verified" if can_read else "no matching ref",
        )

    # ----------------------------------------------------------------------
    # Repository metadata (owner/name/default branch) — no clone
    # ----------------------------------------------------------------------

    @staticmethod
    async def get_repo_metadata(
        *, user_id: str, connection_id: str, repo: str
    ) -> GitHubRepoMetadata:
        repo = _validate_repo_slug(repo)
        private_key, host = await GitHubService._decrypt_key(connection_id=connection_id, user_id=user_id)
        head_res = await _run_git_ssh(
            private_key_text=private_key,
            host=host,
            repo=repo,
            args=["ls-remote", "--symref", "HEAD"],
            timeout=_META_TIMEOUT,
        )
        if head_res["code"] != 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "REPO_UNREACHABLE",
                    "message": (head_res["stderr"] or head_res["stdout"]).strip()[:400],
                },
            )
        default_branch = None
        for ln in (head_res["stdout"] or "").splitlines():
            if ln.startswith("ref:") and "refs/heads/" in ln:
                default_branch = ln.split("refs/heads/")[-1].strip()
                break
        if not default_branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NO_DEFAULT_BRANCH", "message": "Could not resolve default branch"},
            )
        owner, name = repo.split("/", 1)
        return GitHubRepoMetadata(
            repo=repo,
            default_branch=default_branch,
            owner=owner,
            name=name,
            ssh_url=_ssh_url(host, repo),
        )

    # ----------------------------------------------------------------------
    # Clone into a workspace (backend only; never agent-invoked)
    # ----------------------------------------------------------------------

    @staticmethod
    async def clone_into_workspace(
        *,
        user_id: str,
        connection_id: str,
        request: GitHubCloneRequest,
        workspace_path: str,
        server: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Clone ``request.repo`` into ``workspace_path`` on the *remote server*.

        IMPORTANT: the workspace lives on a remote server reached via SSH
        (ServerService/SSHService).  We cannot ``git clone`` directly from
        this backend process unless the backend itself is the server.  To keep
        the security model intact (key never leaves the backend) we:

          1. Decrypt the key in the backend.
          2. Stream the repo over the backend's network using the key, into a
             local temp dir.
          3. Push the bytes to the remote workspace via SSH (tar over ssh).

        For the common single-host dev deployment (backend == server), step 2+3
        collapse to a direct local clone. We detect this via ``server is None``
        OR an explicit local flag and clone directly; otherwise we use the
        stream-and-ship path. Both paths keep the key in the backend only.

        ``server`` is provided by WorkspaceService when the workspace host is a
        distinct machine. When ``server`` is None we assume localhost (the
        backend machine hosts the workspace).
        """
        repo = _validate_repo_slug(request.repo)
        private_key, host = await GitHubService._decrypt_key(connection_id=connection_id, user_id=user_id)
        branch = request.branch
        if branch and not _BRANCH_RE.match(branch):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_BRANCH", "message": "branch contains illegal characters"},
            )
        depth_args = ["--depth", str(request.depth)] if request.depth else []

        ssh_cmd, key_path = _build_ssh_command(private_key, host)
        env = dict(os.environ)
        env["GIT_SSH_COMMAND"] = ssh_cmd
        env["GIT_TERMINAL_PROMPT"] = "0"

        branch_args = ["--branch", branch] if branch else []

        try:
            if server is None:
                # Localhost clone (backend hosts the workspace).
                proc = await asyncio.create_subprocess_exec(
                    "git", "clone", *depth_args, *branch_args,
                    _ssh_url(host, repo), workspace_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
                code = int(proc.returncode or 0)
                if code != 0:
                    return {"ok": False, "code": code, "stderr": (stderr or b"").decode("utf-8", "replace")[:800]}
                return {"ok": True, "code": 0, "stderr": ""}
            else:
                # Remote server: stream the clone over SSH to the workspace.
                # 1) clone to a local temp dir using the key.
                with tempfile.TemporaryDirectory() as tmp:
                    local_repo = os.path.join(tmp, "repo")
                    proc = await asyncio.create_subprocess_exec(
                        "git", "clone", *depth_args, *branch_args,
                        _ssh_url(host, repo), local_repo,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CLONE_TIMEOUT)
                    if int(proc.returncode or 0) != 0:
                        return {"ok": False, "code": int(proc.returncode or 0),
                                "stderr": (stderr or b"").decode("utf-8", "replace")[:800]}
                    # 2) ship the clone to the remote workspace via tar|ssh.
                    from services.ssh_service import SSHService
                    # Build a tar stream locally and pipe it to the remote.
                    tar_proc = await asyncio.create_subprocess_exec(
                        "tar", "-C", local_repo, "-czf", "-", ".",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    # Write into the remote workspace via SSH exec of `tar -xz`.
                    remote_cmd = f"mkdir -p {shlex.quote(workspace_path)} && tar -C {shlex.quote(workspace_path)} -xzf -"
                    ssh_result = await SSHService.execute(
                        server=server, command=remote_cmd
                    )
                    tar_out, tar_err = await tar_proc.communicate()
                    if int(tar_proc.returncode or 0) != 0:
                        return {"ok": False, "code": int(tar_proc.returncode or 0),
                                "stderr": (tar_err or b"").decode("utf-8", "replace")[:800]}
                    if ssh_result.exit_code != 0:
                        return {"ok": False, "code": int(ssh_result.exit_code),
                                "stderr": (ssh_result.stderr or "")[:800]}
                return {"ok": True, "code": 0, "stderr": ""}
        finally:
            try:
                os.unlink(key_path)
            except OSError:
                pass

    # ----------------------------------------------------------------------
    # Pull / Push over an EXISTING workspace repo (agent tool backend)
    # ----------------------------------------------------------------------

    @staticmethod
    async def pull(
        *,
        private_key_text: str,
        host: str,
        workspace_path: str,
        remote: str,
        branch: Optional[str],
        strategy: str,
    ) -> dict[str, Any]:
        if not _REMOTE_RE.match(remote):
            return {"ok": False, "code": 1, "stderr": "invalid remote name"}
        if branch and not _BRANCH_RE.match(branch):
            return {"ok": False, "code": 1, "stderr": "invalid branch name"}

        ssh_cmd, key_path = _build_ssh_command(private_key_text, host)
        env = dict(os.environ)
        env["GIT_SSH_COMMAND"] = ssh_cmd
        env["GIT_TERMINAL_PROMPT"] = "0"

        try:
            # 1) fetch
            fetch = await asyncio.create_subprocess_exec(
                "git", "-C", workspace_path, "fetch", remote,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, fe = await asyncio.wait_for(fetch.communicate(), timeout=_PULL_TIMEOUT)
            if int(fetch.returncode or 0) != 0:
                return {"ok": False, "code": int(fetch.returncode or 0),
                        "stderr": (fe or b"").decode("utf-8", "replace")[:800]}

            # 2) integrate
            if strategy == "ff_only":
                target = branch or "HEAD"
                integ = await asyncio.create_subprocess_exec(
                    "git", "-C", workspace_path, "merge", "--ff-only",
                    f"{remote}/{branch}" if branch else f"{remote}/HEAD",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            elif strategy == "merge":
                integ = await asyncio.create_subprocess_exec(
                    "git", "-C", workspace_path, "merge", "--no-edit",
                    f"{remote}/{branch}" if branch else f"{remote}/HEAD",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            else:  # rebase
                integ = await asyncio.create_subprocess_exec(
                    "git", "-C", workspace_path, "rebase",
                    f"{remote}/{branch}" if branch else f"{remote}/HEAD",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            _, ie = await asyncio.wait_for(integ.communicate(), timeout=_PULL_TIMEOUT)
            code = int(integ.returncode or 0)
            return {"ok": code == 0, "code": code,
                    "stderr": (ie or b"").decode("utf-8", "replace")[:800]}
        finally:
            try:
                os.unlink(key_path)
            except OSError:
                pass

    @staticmethod
    async def push(
        *,
        private_key_text: str,
        host: str,
        workspace_path: str,
        remote: str,
        branch: Optional[str],
        force: bool,
        tags: bool,
    ) -> dict[str, Any]:
        if not _REMOTE_RE.match(remote):
            return {"ok": False, "code": 1, "stderr": "invalid remote name"}
        if branch and not _BRANCH_RE.match(branch):
            return {"ok": False, "code": 1, "stderr": "invalid branch name"}

        ssh_cmd, key_path = _build_ssh_command(private_key_text, host)
        env = dict(os.environ)
        env["GIT_SSH_COMMAND"] = ssh_cmd
        env["GIT_TERMINAL_PROMPT"] = "0"

        try:
            cmd = ["git", "-C", workspace_path, "push"]
            if force:
                # Safer than --force: only force if the remote ref hasn't moved.
                cmd.append("--force-with-lease")
            cmd.append(remote)
            if branch:
                cmd.append(f"HEAD:{branch}")
            if tags:
                cmd.append("--tags")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PUSH_TIMEOUT)
            code = int(proc.returncode or 0)
            return {"ok": code == 0, "code": code,
                    "stderr": (stderr or b"").decode("utf-8", "replace")[:800]}
        finally:
            try:
                os.unlink(key_path)
            except OSError:
                pass
