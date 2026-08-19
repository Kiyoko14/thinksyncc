
import logging
import os
import random
import re
import shlex
import string
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase, get_supabase_async
from models.workspace import WorkspaceResponse
from models.github import GitHubCloneRequest
from models.github_app import GitHubAppCloneRequest
from services.port_allocator import (
    allocate_port as _allocate_port,
    check_port_consistency as _check_consistency,
    release_port as _release_port,
)
from services.redis_service import RedisService
from services.server_service import ServerService
from services.ssh_service import SSHService
from services.workspace_lifecycle import CompensationLedger, CompensationStep

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Slug and domain generation helpers (from slug_service.py)
# --------------------------------------------------------------------------

_NORMALIZED_NAME_RE = re.compile(r"^[a-z0-9]+$")
_RANDOM_SLUG_ALPHABET = string.ascii_lowercase + string.digits
_RANDOM_SLUG_LENGTH = 6
_MAX_NAME_LENGTH = 10
_MAX_SUBDOMAIN_LENGTH = 63
_VALID_SLUG_CHARS = set(string.ascii_lowercase + string.digits + "-")
_MAX_SLUG_LENGTH = 50


def normalize_name(name: str) -> str:
    """Strict workspace name validator. Lowercase alphanumeric, max 10 chars."""
    cleaned = (name or "").strip().lower()
    if not _NORMALIZED_NAME_RE.match(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only letters and numbers allowed",
        )
    if len(cleaned) > _MAX_NAME_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Max 10 characters"
        )
    return cleaned


def generate_random_slug() -> str:
    """Generate a 6-char [a-z0-9] random slug (uniqueness checked by caller)."""
    return "".join(random.choices(_RANDOM_SLUG_ALPHABET, k=_RANDOM_SLUG_LENGTH))


def build_subdomain(normalized_name: str, slug: str) -> str:
    """Build {name}-{slug} subdomain and validate total length ≤ 63."""
    subdomain = f"{normalized_name}-{slug}"
    if len(subdomain) > _MAX_SUBDOMAIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Subdomain too long"
        )
    return subdomain


def sanitize_name_for_slug(name: str) -> str:
    """Sanitize workspace name for slug generation (no uniqueness)."""
    cleaned = name.strip().lower()
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = "".join(c for c in cleaned if c in _VALID_SLUG_CHARS)
    cleaned = cleaned.strip("-")
    if not cleaned:
        cleaned = "workspace"
    return cleaned[:_MAX_SLUG_LENGTH].strip("-") or "workspace"


def generate_slug_from_name(name: str) -> str:
    """Generate a deterministic slug from workspace name (no uniqueness)."""
    return sanitize_name_for_slug(name)


def generate_domain_from_slug(
    *, slug: str, workspace_id: str, base_domain: str = "thinksync.art"
) -> str:
    """Generate deploy domain using {slug}-{short_id}.{base_domain}."""
    if not slug or not all(c in _VALID_SLUG_CHARS for c in slug):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid slug format"
        )
    short_id = (workspace_id or "").replace("-", "").lower()[:6] or "000000"
    return f"{slug}-{short_id}.{base_domain}"


# --------------------------------------------------------------------------
# Redis-based domain assignment (from domain_service.py)
# --------------------------------------------------------------------------


def _ws_domain_key(workspace_id: str) -> str:
    return f"ws:{workspace_id}:domain"


def _domain_lookup_key(subdomain: str) -> str:
    return f"ws_domain:{subdomain.lower().strip()}"


def assign_domain(workspace_id: str, subdomain: str) -> str:
    r = RedisService.get_sync_client()
    if r is None:
        raise RuntimeError("Redis unavailable")

    clean = subdomain.lower().strip()
    domain_key = _ws_domain_key(workspace_id)
    lookup_key = _domain_lookup_key(clean)

    existing_domain = r.get(domain_key)
    if existing_domain is not None:
        return existing_domain

    owner = r.get(lookup_key)
    if owner is not None and owner != workspace_id:
        raise ValueError(
            f"Subdomain '{clean}' is already assigned to workspace {owner}"
        )

    pipeline = r.pipeline()
    pipeline.set(domain_key, clean)
    pipeline.set(lookup_key, workspace_id)
    pipeline.execute()

    logger.info("Assigned subdomain '%s' to workspace %s", clean, workspace_id)
    return clean


def get_workspace_by_domain(subdomain: str) -> Optional[str]:
    r = RedisService.get_sync_client()
    if r is None:
        raise RuntimeError("Redis unavailable")
    return r.get(_domain_lookup_key(subdomain.lower().strip()))


def get_domain(workspace_id: str) -> Optional[str]:
    r = RedisService.get_sync_client()
    if r is None:
        return None
    return r.get(_ws_domain_key(workspace_id))


# --------------------------------------------------------------------------
# Main WorkspaceService
# --------------------------------------------------------------------------


class WorkspaceService:
    _MAX_UNIQUE_ATTEMPTS = 50

    @staticmethod
    def _workspaces_root() -> str:
        """
        Root folder for all workspaces on the remote server.
        Defaults to ~/workspaces (root: /root/workspaces) but can be overridden.
        """
        return "/root/workspaces"

    @staticmethod
    def _sanitize_workspace_name(user_input: str) -> str:
        """Coerce arbitrary input into a strict-compliant name (alphanumeric, max 10)."""
        raw = (user_input or "").strip().lower()
        cleaned = "".join(ch for ch in raw if ("a" <= ch <= "z") or ("0" <= ch <= "9"))
        if not cleaned:
            cleaned = "ws"
        return cleaned[:10]

    @staticmethod
    async def create_workspace_from_prompt(
        *, user_id: str, server_id: str, user_input: str, display_name: str | None = None
    ) -> dict[str, Any]:
        name = WorkspaceService._sanitize_workspace_name(user_input)
        workspace = await WorkspaceService.resolve_workspace(
            user_id=user_id, server_id=server_id, name=name,
            display_name=display_name or user_input,
        )
        slug = str(workspace.get("slug") or name).strip().lower() or "workspace"
        workspace_path = f"{WorkspaceService._workspaces_root()}/{slug}"

        if not workspace_path.startswith("/root/workspaces"):
            workspace_path = f"/root/workspaces/{slug}"

        try:
            os.makedirs(workspace_path, exist_ok=True)
        except Exception:
            pass

        try:
            server = ServerService.get_server(server_id=server_id, user_id=user_id)
            await SSHService.execute(
                server=server, command=f"mkdir -p {shlex.quote(workspace_path)}"
            )
        except Exception:
            pass

        try:
            supabase = await get_supabase_async()
            await supabase.table("workspaces").update({"path": workspace_path}).eq(
                "id", str(workspace.get("id") or "")
            ).execute()
        except Exception:
            pass

        workspace["slug"] = slug
        workspace["path"] = workspace_path
        normalized_name = WorkspaceService._sanitize_workspace_name(
            str(workspace.get("name") or name)
        )
        subdomain = f"{normalized_name}-{slug}"
        workspace["url"] = WorkspaceService._workspace_url(
            f"{subdomain}.{WorkspaceService._base_domain()}"
        )
        return workspace

    @staticmethod
    def _base_domain() -> str:
        settings = get_settings()
        value = (getattr(settings, "WORKSPACE_BASE_DOMAIN", None) or "").strip()
        if not value:
            value = os.getenv("THINKSYNC_WORKSPACE_BASE_DOMAIN", "").strip()
        return value or "thinksync.art"

    @staticmethod
    def _api_error_code(exc: APIError) -> str:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return code.upper()

        first_arg = exc.args[0] if exc.args else None
        if isinstance(first_arg, dict):
            raw_code = first_arg.get("code")
            if isinstance(raw_code, str):
                return raw_code.upper()

        return ""

    @staticmethod
    def _validate_uuid(value: str, field_name: str) -> None:
        try:
            UUID(value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "WORKSPACE_NOT_FOUND"},
            )

    @staticmethod
    def _workspace_path(slug: str) -> str:
        return f"{WorkspaceService._workspaces_root()}/{slug}"

    @staticmethod
    def _workspace_url(domain: str) -> str | None:
        cleaned = (domain or "").strip()
        if not cleaned:
            return None
        return f"https://{cleaned}"

    @staticmethod
    def get_workspace_by_slug(
        *, user_id: str, server_id: str, slug: str
    ) -> dict[str, Any] | None:
        WorkspaceService._validate_uuid(user_id, "user_id")
        WorkspaceService._validate_uuid(server_id, "server_id")
        cleaned_slug = (slug or "").strip().lower()
        if not cleaned_slug:
            return None

        supabase = get_supabase()
        try:
            result = (
                supabase.table("workspaces")
                .select("*")
                .eq("user_id", user_id)
                .eq("server_id", server_id)
                .eq("slug", cleaned_slug)
                .maybe_single()
                .execute()
            )
        except APIError:
            return None

        if not result or not result.data:
            return None
        row = dict(result.data)
        return WorkspaceService._ensure_workspace_fields(
            row, supabase=supabase, user_id=user_id
        )

    @staticmethod
    async def resolve_workspace(
        *, user_id: str, server_id: str, name: str, display_name: str | None = None
    ) -> dict[str, Any]:
        WorkspaceService._validate_uuid(user_id, "user_id")
        WorkspaceService._validate_uuid(server_id, "server_id")

        cleaned_name = (name or "").strip()
        if not cleaned_name:
            cleaned_name = "workspace"

        slug = generate_slug_from_name(cleaned_name)
        existing = WorkspaceService.get_workspace_by_slug(
            user_id=user_id, server_id=server_id, slug=slug
        )
        if existing:
            try:
                _check_consistency(str(existing.get("id") or ""))
            except Exception:
                pass
            try:
                server = ServerService.get_server(server_id=server_id, user_id=user_id)
                workspace_path = str(existing.get("path") or "").strip()
                if workspace_path:
                    mkdir_command = f"mkdir -p {shlex.quote(workspace_path)}"
                    await SSHService.execute(server=server, command=mkdir_command)
            except Exception:
                pass
            return existing

        created = await WorkspaceService.create_workspace(
            user_id=user_id, server_id=server_id, name=cleaned_name,
            display_name=display_name or cleaned_name,
        )
        return created.model_dump(mode="python")

    @staticmethod
    def _unique_random_slug(*, supabase) -> str:
        for _ in range(WorkspaceService._MAX_UNIQUE_ATTEMPTS):
            candidate = generate_random_slug()
            try:
                result = (
                    supabase.table("workspaces")
                    .select("id")
                    .eq("slug", candidate)
                    .limit(1)
                    .execute()
                )
                if not result.data:
                    return candidate
            except Exception:
                return candidate
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to generate unique slug",
        )

    @staticmethod
    def _unique_slug(*, supabase, server_id: str, base_slug: str) -> str:
        base = (base_slug or "").strip().lower()
        if not base:
            base = "workspace"

        def exists(candidate: str) -> bool:
            try:
                result = (
                    supabase.table("workspaces")
                    .select("id")
                    .eq("server_id", server_id)
                    .eq("slug", candidate)
                    .limit(1)
                    .execute()
                )
                if result.data:
                    return True
            except Exception:
                pass

            derived_domain = f"{candidate}.{WorkspaceService._base_domain()}"
            try:
                domain_result = (
                    supabase.table("workspaces")
                    .select("id")
                    .eq("domain", derived_domain)
                    .limit(1)
                    .execute()
                )
                return bool(domain_result.data)
            except Exception:
                return False

        if not exists(base):
            return base

        for i in range(2, WorkspaceService._MAX_UNIQUE_ATTEMPTS + 1):
            candidate = f"{base}-{i}"
            if not exists(candidate):
                return candidate

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace slug already exists on this server",
        )

    @staticmethod
    async def create_workspace(
        user_id: str,
        server_id: str,
        name: str,
        github_connection_id: str | None = None,
        github_clone: "GitHubCloneRequest | None" = None,
        app_clone: "GitHubAppCloneRequest | None" = None,
        # Original, human-readable workspace name. Kept separate from the
        # sanitized `name`/`slug`. Falls back to `name` when None.
        display_name: str | None = None,
    ) -> WorkspaceResponse:
        WorkspaceService._validate_uuid(user_id, "user_id")
        WorkspaceService._validate_uuid(server_id, "server_id")

        cleaned_name = name.strip()
        if not cleaned_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workspace name is required",
            )

        server = ServerService.get_server(server_id=server_id, user_id=user_id)
        supabase = await get_supabase_async()

        # Resolve the GitHub linkage. Exactly one transport may be selected:
        #   * SSH connection + clone request  -> GitHubService.clone_into_workspace
        #   * App installation clone request   -> GitHubAppService.clone (token)
        # ThinkSync (plain) workspaces have neither.
        github_link_id: str | None = None
        is_app_workspace = False
        if app_clone is not None:
            # GitHub App workspace: we create a github_connections row with
            # auth_method='app' (no SSH keys) so the existing discriminator
            # (github_connection_id IS NULL -> ThinkSync) keeps working and the
            # workspace can later be re-identified as an App workspace.
            is_app_workspace = True
        elif github_connection_id and github_clone is not None:
            WorkspaceService._validate_uuid(github_connection_id, "github_connection_id")
            github_link_id = github_connection_id

        base_slug = generate_slug_from_name(cleaned_name)

        existing = WorkspaceService.get_workspace_by_slug(
            user_id=user_id, server_id=server_id, slug=base_slug
        )
        if existing:
            # Duplicate policy: a workspace with this name already exists on the
            # server -> 409 Conflict (do NOT silently return the existing one).
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "WORKSPACE_NAME_CONFLICT",
                    "message": "A workspace with this name already exists on this server.",
                },
            )

        try:
            by_name = (
                await supabase.table("workspaces")
                .select("*")
                .eq("user_id", user_id)
                .eq("server_id", server_id)
                .eq("name", cleaned_name)
                .order("created_at", desc=True)
                .limit(1)
                .maybe_single()
                .execute()
            )
            if by_name and by_name.data:
                # Duplicate policy: same name on same server -> 409 Conflict.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "WORKSPACE_NAME_CONFLICT",
                        "message": "A workspace with this name already exists on this server.",
                    },
                )
        except HTTPException:
            raise
        except Exception:
            pass

        workspace_id = str(uuid4())
        normalized_name = (
            WorkspaceService._sanitize_workspace_name(cleaned_name) or base_slug or "ws"
        )
        slug = WorkspaceService._unique_random_slug(supabase=supabase)
        subdomain = build_subdomain(normalized_name, slug)
        domain = f"{subdomain}.{WorkspaceService._base_domain()}"
        workspace_path = WorkspaceService._workspace_path(slug)

        # ── CREATE Saga (Part 5) ────────────────────────────────────────────
        # Steps with external side effects run through the Part 4
        # CompensationLedger (the single rollback mechanism — no new mechanism
        # is introduced). On any step failure, completed steps compensate in
        # reverse order and the ORIGINAL error is re-raised, leaving NO
        # half-created object. Saga boundary (approved):
        #     remote_mkdir -> create_app_connection -> create_workspace_row
        #     -> clone_repository
        # allocate_port / assign_domain remain OUTSIDE the saga (post-commit,
        # best-effort; Redis is not a source of truth).
        record = {
            "id": workspace_id,
            "user_id": user_id,
            "server_id": server_id,
            "name": cleaned_name,
            # Preserve the user's original name exactly. Never sanitize/slugify.
            "display_name": display_name or cleaned_name,
            "path": workspace_path,
            "slug": slug,
            "domain": domain,
            "github_connection_id": github_link_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        # Mutable holders so step closures can publish results to the outer scope.
        insert_holder: dict[str, Any] = {}
        conn_id_holder: dict[str, str] = {}

        ledger = CompensationLedger()

        # ---- Step 1: remote_mkdir (compensate: rm -rf, path-guarded) --------
        async def _exec_mkdir() -> Any:
            await SSHService.execute(
                server=server, command=f"mkdir -p {shlex.quote(workspace_path)}"
            )
            return {"path": workspace_path}

        async def _comp_mkdir() -> Any:
            from services.workspace_lifecycle import _assert_safe_workspace_path

            _assert_safe_workspace_path(workspace_path)
            await SSHService.execute(
                server=server, command=f"rm -rf {shlex.quote(workspace_path)}"
            )
            return {"removed": workspace_path}

        await ledger.run(
            CompensationStep(
                name="remote_mkdir",
                execute=_exec_mkdir,
                compensate=_comp_mkdir,
                metadata={"workspace_id": workspace_id, "path": workspace_path},
            )
        )

        # ---- Step 2: create_app_connection (App only) ----------------------
        if is_app_workspace:
            conn_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn_record = {
                "id": conn_id,
                "user_id": user_id,
                "name": f"app:{app_clone.installation_id}",
                "auth_method": "app",
                "host": "github.com",
                "ssh_public_key": None,
                "ssh_private_key": None,
                "ssh_key_type": None,
                # installation_id is the production source of truth (dedicated
                # column). The name prefix is retained only as a human-readable
                # label; credential resolution reads this column, never the name.
                "installation_id": app_clone.installation_id,
                # Canonical (immutable) repo id + mutable full_name so webhook
                # events (repository.renamed / deleted / transfer) map back here.
                "repo_id": app_clone.repo_id,
                "repo_full_name": app_clone.repo,
                "created_at": now,
                "updated_at": now,
            }

            async def _exec_app_conn() -> Any:
                try:
                    await supabase.table("github_connections").insert(conn_record).execute()
                except HTTPException:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[workspace] app_connection_insert_failed | ws=%s", workspace_id)
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail={"code": "GITHUB_APP_CONNECTION_FAILED", "message": str(exc)},
                    )
                conn_id_holder["id"] = conn_id
                record["github_connection_id"] = conn_id
                return {"connection_id": conn_id}

            async def _comp_app_conn() -> Any:
                await supabase.table("github_connections").delete().eq("id", conn_id).execute()
                return {"deleted_connection": conn_id}

            await ledger.run(
                CompensationStep(
                    name="create_app_connection",
                    execute=_exec_app_conn,
                    compensate=_comp_app_conn,
                    metadata={"workspace_id": workspace_id, "connection_id": conn_id},
                )
            )

        # ---- Step 3: create_workspace_row ----------------------------------
        async def _exec_ws_row() -> Any:
            try:
                result = await supabase.table("workspaces").insert(record).execute()
            except APIError as exc:
                code = WorkspaceService._api_error_code(exc)
                if code == "23505":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "WORKSPACE_NAME_CONFLICT",
                            "message": "A workspace with this name already exists on this server.",
                        },
                    )
                if code == "23503":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Server not found or access denied",
                    )
                if code == "22P02":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid request data",
                    )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create workspace",
                )
            if not result or not result.data:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create workspace",
                )
            insert_holder["result"] = result
            return {"workspace_id": workspace_id}

        async def _comp_ws_row() -> Any:
            await supabase.table("workspaces").delete().eq("id", workspace_id).execute()
            return {"deleted_workspace": workspace_id}

        await ledger.run(
            CompensationStep(
                name="create_workspace_row",
                execute=_exec_ws_row,
                compensate=_comp_ws_row,
                metadata={"workspace_id": workspace_id},
            )
        )

        # ---- Step 4: clone_repository (compensate=None; Step 1 rm -rf covers) 
        async def _exec_clone() -> Any:
            # The agent NEVER clones. When a connection + clone request are
            # present, the backend clones the repo directly into the workspace
            # path so it is a real git checkout for later agent git_* ops.
            if is_app_workspace and app_clone is not None:
                try:
                    from services.github_app_service import GitHubAppService

                    clone_res = await GitHubAppService.clone(
                        installation_id=app_clone.installation_id,
                        repo=app_clone.repo,
                        branch=app_clone.branch,
                        depth=app_clone.depth,
                        workspace_path=workspace_path,
                        server=server,
                    )
                    if not clone_res.get("ok"):
                        logger.error(
                            "[workspace] github_app_clone_failed | ws=%s | code=%s | stderr=%s",
                            workspace_id,
                            clone_res.get("code"),
                            clone_res.get("stderr", "")[:400],
                        )
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail={
                                "code": "GITHUB_CLONE_FAILED",
                                "message": (clone_res.get("stderr") or "GitHub clone failed")[:400],
                            },
                        )
                except HTTPException:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[workspace] github_app_clone_error | ws=%s", workspace_id)
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail={"code": "GITHUB_CLONE_ERROR", "message": str(exc)[:400]},
                    )
            elif github_link_id and github_clone is not None:
                try:
                    from services.github_service import GitHubService

                    clone_res = await GitHubService.clone_into_workspace(
                        user_id=user_id,
                        connection_id=github_link_id,
                        request=github_clone,
                        workspace_path=workspace_path,
                        server=server,
                    )
                    if not clone_res.get("ok"):
                        logger.error(
                            "[workspace] github_clone_failed | ws=%s | code=%s | stderr=%s",
                            workspace_id,
                            clone_res.get("code"),
                            clone_res.get("stderr", "")[:400],
                        )
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail={
                                "code": "GITHUB_CLONE_FAILED",
                                "message": (clone_res.get("stderr") or "GitHub clone failed")[:400],
                            },
                        )
                except HTTPException:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[workspace] github_clone_error | ws=%s", workspace_id)
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail={"code": "GITHUB_CLONE_ERROR", "message": str(exc)[:400]},
                    )
            return {"cloned": is_app_workspace or bool(github_link_id and github_clone)}

        # compensate=None: clone artefacts live inside workspace_path, which the
        # remote_mkdir compensation (rm -rf) removes. No separate clone rollback.
        await ledger.run(
            CompensationStep(
                name="clone_repository",
                execute=_exec_clone,
                compensate=None,
                metadata={"workspace_id": workspace_id},
            )
        )

        result = insert_holder["result"]

        payload = dict(result.data[0])
        payload["url"] = WorkspaceService._workspace_url(
            str(payload.get("domain") or "")
        )

        try:
            _allocate_port(workspace_id)
        except Exception:
            logger.warning(
                "Port allocation failed for workspace %s — Redis may be unavailable",
                workspace_id,
            )

        try:
            assign_domain(workspace_id, subdomain)
        except Exception:
            logger.warning(
                "Domain assignment failed for workspace %s — Redis may be unavailable",
                workspace_id,
            )

        return WorkspaceResponse(**payload)

    @staticmethod
    def get_workspaces(
        user_id: str, server_id: str | None = None
    ) -> list[WorkspaceResponse]:
        WorkspaceService._validate_uuid(user_id, "user_id")
        if server_id is not None:
            WorkspaceService._validate_uuid(server_id, "server_id")

        supabase = get_supabase()
        query = (
            supabase.table("workspaces")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
        )
        if server_id is not None:
            query = query.eq("server_id", server_id)

        result = query.execute()
        rows = result.data or []
        hydrated: list[WorkspaceResponse] = []
        for row in rows:
            repaired = WorkspaceService._ensure_workspace_fields(
                dict(row), supabase=supabase, user_id=user_id
            )
            hydrated.append(WorkspaceResponse(**repaired))
        return hydrated

    @staticmethod
    def get_workspace_by_id(id: str, user_id: str) -> dict[str, Any]:
        if not isinstance(id, str) or not id.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "WORKSPACE_REQUIRED"},
            )
        WorkspaceService._validate_uuid(id, "workspace_id")
        WorkspaceService._validate_uuid(user_id, "user_id")

        supabase = get_supabase()
        try:
            result = (
                supabase.table("workspaces")
                .select("*")
                .eq("id", id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except APIError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "WORKSPACE_NOT_FOUND"},
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "WORKSPACE_NOT_FOUND"},
            )

        row = dict(result.data)
        return WorkspaceService._ensure_workspace_fields(
            row, supabase=supabase, user_id=user_id
        )

    @staticmethod
    async def delete_workspace(*, workspace_id: str, user_id: str) -> dict[str, Any]:
        """Delete a workspace via the lifecycle orchestrator (Part 4).

        Thin adapter: all business logic (validation, remote cleanup, orphan
        connection cleanup, audit, cache invalidation, compensation) lives in
        WorkspaceLifecycleOrchestrator — the single entry point for lifecycle
        transitions. This method exists only to keep the router surface stable.
        """
        from services.workspace_lifecycle import (
            LifecycleAction,
            LifecycleContext,
            WorkspaceLifecycleOrchestrator,
        )

        result = await WorkspaceLifecycleOrchestrator.transition(
            ctx=LifecycleContext(
                action=LifecycleAction.DELETE,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        )
        return {"action": result.action, "workspace_id": result.workspace_id, "success": result.success}

    @staticmethod
    async def disconnect_repository(*, workspace_id: str, user_id: str) -> dict[str, Any]:
        """Disconnect a GitHub repo from a workspace via the orchestrator (Part 4).

        The workspace remains (reverts to a ThinkSync workspace); an orphaned
        App connection is cleaned up. Delegates to the lifecycle orchestrator.
        """
        from services.workspace_lifecycle import (
            LifecycleAction,
            LifecycleContext,
            WorkspaceLifecycleOrchestrator,
        )

        result = await WorkspaceLifecycleOrchestrator.transition(
            ctx=LifecycleContext(
                action=LifecycleAction.DISCONNECT,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        )
        return {"action": result.action, "workspace_id": result.workspace_id, "success": result.success}

    @staticmethod
    def ensure_workspace_domain(
        *, workspace_id: str, user_id: str, desired_domain: str
    ) -> tuple[str, bool]:
        WorkspaceService._validate_uuid(workspace_id, "workspace_id")
        WorkspaceService._validate_uuid(user_id, "user_id")

        supabase = get_supabase()
        try:
            result = (
                supabase.table("workspaces")
                .select("id,domain")
                .eq("id", workspace_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except Exception:
            result = None

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "WORKSPACE_NOT_FOUND"},
            )

        existing = (result.data.get("domain") or "").strip()
        if existing:
            return existing, True

        cleaned = (desired_domain or "").strip().lower()
        if not cleaned:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_domain", "message": "Invalid domain"},
            )

        def domain_taken(value: str) -> bool:
            try:
                check = (
                    supabase.table("workspaces")
                    .select("id")
                    .eq("domain", value)
                    .limit(1)
                    .execute()
                )
                return bool(check.data)
            except Exception:
                return False

        candidate = cleaned
        if domain_taken(candidate):
            left, _, rest = candidate.partition(".")
            short_id = (workspace_id or "").replace("-", "").lower()[:6] or "000000"
            if rest:
                candidate = f"{left}-{short_id}.{rest}"
            else:
                candidate = f"{left}-{short_id}"

        try:
            supabase.table("workspaces").update({"domain": candidate}).eq(
                "id", workspace_id
            ).eq("user_id", user_id).execute()
        except APIError as exc:
            code = WorkspaceService._api_error_code(exc)
            if code == "23505" and candidate == cleaned:
                left, _, rest = cleaned.partition(".")
                short_id = (workspace_id or "").replace("-", "").lower()[:6] or "000000"
                alt = f"{left}-{short_id}.{rest}" if rest else f"{left}-{short_id}"
                supabase.table("workspaces").update({"domain": alt}).eq(
                    "id", workspace_id
                ).eq("user_id", user_id).execute()
                return alt, False
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "domain_conflict",
                    "message": "Workspace domain already exists",
                },
            )
        except Exception:
            return candidate, False

        return candidate, False

    @staticmethod
    def _ensure_workspace_fields(
        row: dict[str, Any], *, supabase, user_id: str
    ) -> dict[str, Any]:
        slug = (row.get("slug") or "").strip()
        domain = (row.get("domain") or "").strip()
        path = (row.get("path") or "").strip()

        # Ensure display_name always exists (fall back to sanitized name for
        # legacy rows that predate the display_name column).
        if not row.get("display_name"):
            row["display_name"] = row.get("name") or "workspace"

        if slug and domain and path:
            if not path.startswith("/root/workspaces"):
                path = WorkspaceService._workspace_path(slug)
                patch = {"path": path}
                try:
                    supabase.table("workspaces").update(patch).eq(
                        "id", str(row.get("id") or "")
                    ).execute()
                except Exception:
                    pass
                row.update(patch)
            row["url"] = WorkspaceService._workspace_url(domain)
            return row

        server_id = str(row.get("server_id") or "")
        workspace_id = str(row.get("id") or "")
        name = str(row.get("name") or "workspace")

        patch: dict[str, Any] = {}
        if not slug:
            base_slug = generate_slug_from_name(name)
            slug = WorkspaceService._unique_slug(
                supabase=supabase, server_id=server_id, base_slug=base_slug
            )
            patch["slug"] = slug

        if not domain:
            domain = f"{slug}.{WorkspaceService._base_domain()}"
            patch["domain"] = domain

        if not path:
            path = WorkspaceService._workspace_path(slug)
            patch["path"] = path
        elif not path.startswith("/root/workspaces"):
            path = WorkspaceService._workspace_path(slug)
            patch["path"] = path

        if patch:
            try:
                supabase.table("workspaces").update(patch).eq("id", workspace_id).execute()
            except Exception:
                pass
            row.update(patch)

        row["url"] = WorkspaceService._workspace_url(domain)
        return row
