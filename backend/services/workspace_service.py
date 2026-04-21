import logging
import os
import shlex
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from models.workspace import WorkspaceResponse
from services.domain_service import assign_domain as _redis_assign_domain
from services.port_allocator import allocate_port as _allocate_port, check_port_consistency as _check_consistency, release_port as _release_port
from services.server_service import ServerService
from services.slug_service import (
    SlugService,
    build_subdomain as _build_subdomain,
    generate_random_slug as _generate_random_slug,
)
from services.ssh_service import SSHService


logger = logging.getLogger(__name__)


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
    async def create_workspace_from_prompt(*, user_id: str, server_id: str, user_input: str) -> dict[str, Any]:
        name = WorkspaceService._sanitize_workspace_name(user_input)
        workspace = await WorkspaceService.resolve_workspace(user_id=user_id, server_id=server_id, name=name)
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
            await SSHService.execute(server=server, command=f"mkdir -p {shlex.quote(workspace_path)}")
        except Exception:
            pass

        try:
            supabase = get_supabase()
            supabase.table("workspaces").update({"path": workspace_path}).eq("id", str(workspace.get("id") or "")).execute()
        except Exception:
            pass

        workspace["slug"] = slug
        workspace["path"] = workspace_path
        workspace["url"] = WorkspaceService._workspace_url(str(workspace.get("domain") or ""))
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
        # Deterministic project root for all workspaces.
        return f"{WorkspaceService._workspaces_root()}/{slug}"

    @staticmethod
    def _workspace_url(domain: str) -> str | None:
        cleaned = (domain or "").strip()
        if not cleaned:
            return None
        return f"https://{cleaned}"

    @staticmethod
    def get_workspace_by_slug(*, user_id: str, server_id: str, slug: str) -> dict[str, Any] | None:
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
        return WorkspaceService._ensure_workspace_fields(row, supabase=supabase, user_id=user_id)

    @staticmethod
    async def resolve_workspace(*, user_id: str, server_id: str, name: str) -> dict[str, Any]:
        """
        Resolve a workspace by deterministic slug (derived from name).
        If it exists, return it. Otherwise create a new workspace.
        """
        WorkspaceService._validate_uuid(user_id, "user_id")
        WorkspaceService._validate_uuid(server_id, "server_id")

        cleaned_name = (name or "").strip()
        if not cleaned_name:
            cleaned_name = "workspace"

        slug = SlugService.generate_slug(cleaned_name)
        existing = WorkspaceService.get_workspace_by_slug(user_id=user_id, server_id=server_id, slug=slug)
        if existing:
            # Run consistency check on startup so port state is always valid.
            try:
                _check_consistency(str(existing.get("id") or ""))
            except Exception:
                pass
            # Ensure remote folder exists for reliable writes/execution.
            try:
                server = ServerService.get_server(server_id=server_id, user_id=user_id)
                workspace_path = str(existing.get("path") or "").strip()
                if workspace_path:
                    mkdir_command = f"mkdir -p {shlex.quote(workspace_path)}"
                    await SSHService.execute(server=server, command=mkdir_command)
            except Exception:
                pass
            return existing

        created = await WorkspaceService.create_workspace(user_id=user_id, server_id=server_id, name=cleaned_name)
        return created.model_dump(mode="python")

    @staticmethod
    def _unique_random_slug(*, supabase) -> str:
        """Generate a globally unique 6-char random slug."""
        for _ in range(WorkspaceService._MAX_UNIQUE_ATTEMPTS):
            candidate = _generate_random_slug()
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
            # Keep server-scoped slug uniqueness for workspace lookup.
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

            # Also ensure the derived subdomain is globally unique to avoid duplicate domains.
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
                # Backward compatibility if the domain column doesn't exist yet.
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
    async def create_workspace(user_id: str, server_id: str, name: str) -> WorkspaceResponse:
        WorkspaceService._validate_uuid(user_id, "user_id")
        WorkspaceService._validate_uuid(server_id, "server_id")

        cleaned_name = name.strip()
        if not cleaned_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workspace name is required",
            )

        server = ServerService.get_server(server_id=server_id, user_id=user_id)
        supabase = get_supabase()

        base_slug = SlugService.generate_slug(cleaned_name)

        # Idempotency / UX: if this workspace already exists (same user+server+slug), reuse it.
        existing = WorkspaceService.get_workspace_by_slug(user_id=user_id, server_id=server_id, slug=base_slug)
        if existing:
            existing["url"] = WorkspaceService._workspace_url(str(existing.get("domain") or ""))
            return WorkspaceResponse(**existing)

        # Also reuse by exact name (prevents accidental duplicates from double-clicks / retries).
        try:
            by_name = (
                supabase.table("workspaces")
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
                row = WorkspaceService._ensure_workspace_fields(dict(by_name.data), supabase=supabase, user_id=user_id)
                row["url"] = WorkspaceService._workspace_url(str(row.get("domain") or ""))
                return WorkspaceResponse(**row)
        except Exception:
            pass

        workspace_id = str(uuid4())
        normalized_name = WorkspaceService._sanitize_workspace_name(cleaned_name) or base_slug or "ws"
        slug = WorkspaceService._unique_random_slug(supabase=supabase)
        subdomain = _build_subdomain(normalized_name, slug)
        domain = f"{subdomain}.{WorkspaceService._base_domain()}"
        workspace_path = WorkspaceService._workspace_path(slug)

        mkdir_command = f"mkdir -p {shlex.quote(workspace_path)}"
        await SSHService.execute(server=server, command=mkdir_command)

        record = {
            "id": workspace_id,
            "user_id": user_id,
            "server_id": server_id,
            "name": cleaned_name,
            "path": workspace_path,
            "slug": slug,
            "domain": domain,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = supabase.table("workspaces").insert(record).execute()
        except APIError as exc:
            code = WorkspaceService._api_error_code(exc)
            if code == "23505":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Workspace slug already exists on this server",
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

        payload = dict(result.data[0])
        payload["url"] = WorkspaceService._workspace_url(str(payload.get("domain") or ""))

        # Allocate an immutable port for this workspace and persist the subdomain
        # mapping in Redis so the gateway can resolve it.
        try:
            _allocate_port(workspace_id)
        except Exception:
            logger.warning("Port allocation failed for workspace %s — Redis may be unavailable", workspace_id)

        try:
            _redis_assign_domain(workspace_id, subdomain)
        except Exception:
            logger.warning("Domain assignment failed for workspace %s — Redis may be unavailable", workspace_id)

        return WorkspaceResponse(**payload)

    @staticmethod
    def get_workspaces(user_id: str, server_id: str | None = None) -> list[WorkspaceResponse]:
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
            repaired = WorkspaceService._ensure_workspace_fields(dict(row), supabase=supabase, user_id=user_id)
            hydrated.append(WorkspaceResponse(**repaired))
        return hydrated

    @staticmethod
    def get_workspace_by_id(id: str, user_id: str) -> dict[str, Any]:
        if not isinstance(id, str) or not id.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})
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
        return WorkspaceService._ensure_workspace_fields(row, supabase=supabase, user_id=user_id)

    @staticmethod
    def ensure_workspace_domain(*, workspace_id: str, user_id: str, desired_domain: str) -> tuple[str, bool]:
        """
        Ensure a workspace has a persisted domain.

        Returns: (domain, reused_domain)
        - reused_domain=True when an existing domain was already present and reused.
        - reused_domain=False when the domain was missing and was written.
        """
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
                check = supabase.table("workspaces").select("id").eq("domain", value).limit(1).execute()
                return bool(check.data)
            except Exception:
                return False

        candidate = cleaned
        if domain_taken(candidate):
            # Avoid duplicates by suffixing with a stable short id.
            left, _, rest = candidate.partition(".")
            short_id = (workspace_id or "").replace("-", "").lower()[:6] or "000000"
            if rest:
                candidate = f"{left}-{short_id}.{rest}"
            else:
                candidate = f"{left}-{short_id}"

        try:
            supabase.table("workspaces").update({"domain": candidate}).eq("id", workspace_id).eq("user_id", user_id).execute()
        except APIError as exc:
            code = WorkspaceService._api_error_code(exc)
            if code == "23505" and candidate == cleaned:
                left, _, rest = cleaned.partition(".")
                short_id = (workspace_id or "").replace("-", "").lower()[:6] or "000000"
                alt = f"{left}-{short_id}.{rest}" if rest else f"{left}-{short_id}"
                supabase.table("workspaces").update({"domain": alt}).eq("id", workspace_id).eq("user_id", user_id).execute()
                return alt, False
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "domain_conflict", "message": "Workspace domain already exists"},
            )
        except Exception:
            # If persistence fails, still return the candidate, but caller should treat deployment as best-effort.
            return candidate, False

        return candidate, False

    @staticmethod
    def _ensure_workspace_fields(
        row: dict[str, Any],
        *,
        supabase,
        user_id: str,
    ) -> dict[str, Any]:
        slug = (row.get("slug") or "").strip()
        domain = (row.get("domain") or "").strip()
        path = (row.get("path") or "").strip()

        if slug and domain and path:
            if not path.startswith("/root/workspaces"):
                path = WorkspaceService._workspace_path(slug)
                patch = {"path": path}
                try:
                    supabase.table("workspaces").update(patch).eq("id", str(row.get("id") or "")).execute()
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
            base_slug = SlugService.generate_slug(name)
            slug = WorkspaceService._unique_slug(supabase=supabase, server_id=server_id, base_slug=base_slug)
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
                # Do not block reads; caller still gets a consistent view.
                pass
            row.update(patch)

        row["url"] = WorkspaceService._workspace_url(domain)
        return row
