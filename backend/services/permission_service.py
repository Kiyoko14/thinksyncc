"""PermissionService — single gate for all write/execute actions.

Every write operation (file write, command execution, deployment) must pass
through `PermissionService.check()` before proceeding.

This is the ONLY place where allow_write is interpreted.
All other services MUST call PermissionService and must NOT force allow_write themselves.

Design:
  - Single async method: check(intent, action, user_id, workspace_id, server_id)
  - Returns (allowed: bool, reason: str)
  - Enforces workspace locks, server access rights, and global safety switches
  - All "allow_write = True" forced overrides in agent_service.py / executor.py
    are replaced by a single call to this service.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from core.database import get_supabase
from core.authorization import assert_owns
from models.job import JobStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single source of truth for write permission
# ---------------------------------------------------------------------------


class PermissionService:
    """Centralized permission gate for all mutating actions."""

    @staticmethod
    def check(
        *,
        intent: str,
        action: str,
        user_id: str,
        workspace_id: str | None,
        server_id: str | None,
        job_id: str | None = None,
    ) -> tuple[bool, str]:
        """Check whether the requested action is permitted.

        Args:
            intent: "chat" | "code" | "server"
            action: "write_file" | "run_command" | "deploy" | "restart_service"
            user_id: owner of the job
            workspace_id: target workspace (may be None for cross-workspace actions)
            server_id: target server
            job_id: optional job ID for audit trail

        Returns:
            (True, "") if allowed
            (False, reason) if denied
        """
        settings = get_settings()

        # ------------------------------------------------------------------
        # 1. Global kill-switch (env override)
        # ------------------------------------------------------------------
        if not settings.AGENT_ALLOW_WRITE:
            reason = "Global write switch (AGENT_ALLOW_WRITE) is disabled."
            logger.warning(
                "[permission] denied | job=%s | action=%s | reason=%s",
                job_id or "<unknown>",
                action,
                reason,
            )
            return False, reason

        # ------------------------------------------------------------------
        # 2. Workspace ownership check
        # ------------------------------------------------------------------
        if workspace_id:
            try:
                ws = (
                    get_supabase()
                    .table("workspaces")
                    .select("user_id, server_id")
                    .eq("id", workspace_id)
                    .maybe_single()
                    .execute()
                )
                if not ws or not ws.data:
                    return False, f"Workspace {workspace_id} not found."
                # Centralized, fail-closed ownership check (D5).
                try:
                    assert_owns(ws.data.get("user_id"), user_id, "Workspace")
                except Exception:
                    return False, "Workspace does not belong to the calling user."
                if server_id and ws.data.get("server_id") != server_id:
                    return False, "Workspace server_id does not match requested server_id."
            except Exception as exc:
                logger.warning("[permission] workspace check failed: %s", exc)
                # Fail closed on DB errors
                return False, f"Could not verify workspace ownership: {exc}"

        # ------------------------------------------------------------------
        # 3. Server access check
        # ------------------------------------------------------------------
        if server_id:
            try:
                srv = (
                    get_supabase()
                    .table("servers")
                    .select("user_id")
                    .eq("id", server_id)
                    .maybe_single()
                    .execute()
                )
                if not srv or not srv.data:
                    return False, f"Server {server_id} not found."
                # Centralized, fail-closed ownership check (D5).
                try:
                    assert_owns(srv.data.get("user_id"), user_id, "Server")
                except Exception:
                    return False, "Server does not belong to the calling user."
            except Exception as exc:
                logger.warning("[permission] server check failed: %s", exc)
                return False, f"Could not verify server ownership: {exc}"

        # ------------------------------------------------------------------
        # 4. Log the permission decision for audit
        # ------------------------------------------------------------------
        logger.info(
            "[permission] allowed | job=%s | intent=%s | action=%s | user=%s | workspace=%s",
            job_id or "<unknown>",
            intent,
            action,
            user_id,
            workspace_id,
        )
        return True, ""

    @staticmethod
    async def check_async(
        *,
        intent: str,
        action: str,
        user_id: str,
        workspace_id: str | None,
        server_id: str | None,
        job_id: str | None = None,
    ) -> tuple[bool, str]:
        """Async wrapper — runs the sync check in a thread pool.

        Use this from async code paths to avoid blocking the event loop on DB calls.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        # `run_in_executor` forwards positional args only, and the sync
        # `PermissionService.check` is keyword-only. Bind the kwargs with a
        # lambda so the DB call still runs off the event loop in a thread pool.
        return await loop.run_in_executor(
            None,
            lambda: PermissionService.check(
                intent=intent,
                action=action,
                user_id=user_id,
                workspace_id=workspace_id,
                server_id=server_id,
                job_id=job_id,
            ),
        )
