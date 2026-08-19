"""ExecutionResultProjector — single owner of the final execution response.

This component replaces the scattered, lossy summary logic that previously
collapsed every successful job into the generic "All set." fallback
(``run_agent_pipeline`` read ``result.get("summary") or result.get("message")``
which were almost always empty).

Responsibility
--------------
Take the INTERNAL execution result (the enriched ``result`` dict from
``_run_code_execution``), the resolved workspace row, and the user objective,
then project THREE deterministic, structured views:

1. ``user_summary``      — professional markdown for the user-facing response
                           (HTTP / Telegram / Web). Never "All set.".
2. ``conversation_summary`` — structured completion record stored in chat
                           history so future turns know what was done.
3. ``context_state``     — last-execution state persisted for ``ContextEngine``
                           (template / strategy / recent files / status).

Design rules
------------
* Read-only projection. It does NOT execute tools, does NOT call the LLM, and
  does NOT reconstruct information from live progress events. ExecutionResult
  is the single source of truth.
* Deterministic. The same input always yields the same output.
* Backward-compatible. If the internal result carries a legacy ``summary`` or
  ``message`` field, it is honored (with the richer structured view appended).
"""

from __future__ import annotations

from typing import Any

from services.execution_result import ExecutionResult


class ExecutionResultProjector:
    @staticmethod
    def project(
        result: dict[str, Any],
        *,
        workspace: dict[str, Any] | None = None,
        objective: str | None = None,
        workspace_url: str | None = None,
    ) -> ExecutionResult:
        """Build the single ``ExecutionResult`` from internal execution data.

        Args:
            result: enriched execution result dict from ``_run_code_execution``.
            workspace: resolved workspace row (has ``display_name``, ``slug``,
                ``id``, ``path``).
            objective: the user's original request (used only as a fallback
                title when no workspace display name is available).
            workspace_url: verified deployment URL, if any.
        """
        result = result or {}
        workspace = workspace or {}

        display_name = (
            workspace.get("display_name")
            or workspace.get("name")
            or (objective or "").strip()
            or "workspace"
        )
        slug = workspace.get("slug") or ""
        workspace_id = str(workspace.get("id") or result.get("workspace_id") or "")

        success = bool(result.get("success", True))
        status = "completed" if success else "failed"

        # Legacy fields (honored if present, for backward compatibility).
        legacy_summary = str(result.get("summary") or result.get("message") or "").strip()

        # --- Build the structured ExecutionResult --------------------------
        er = ExecutionResult(
            workspace_id=workspace_id,
            workspace_display_name=display_name,
            workspace_slug=slug,
            workspace_path=str(workspace.get("path") or result.get("workspace_path") or ""),
            strategy=str(result.get("strategy") or ""),
            template_name=result.get("template_name"),
            template_summary=result.get("template_summary"),
            created_files=list(result.get("created_files") or result.get("files") or []),
            modified_files=list(result.get("modified_files") or []),
            deleted_files=list(result.get("deleted_files") or []),
            success=success,
            status=status,
            tests=result.get("tests"),
            warnings=list(result.get("warnings") or []),
            errors=list(result.get("errors") or []) if result.get("errors") else None,
            patches=list(result.get("patches") or []) if result.get("patches") else None,
            deployment=result.get("deployment") or None,
            artifacts=result.get("artifacts"),
            logs_tail=str((result.get("logs") or ""))[-800:] or None,
        )

        # --- Build the user-facing summary (deterministic) ----------------
        er.summary = ExecutionResultProjector._build_user_summary(er, legacy_summary)
        return er

    # ------------------------------------------------------------------
    # Summary builders
    # ------------------------------------------------------------------
    @staticmethod
    def _build_user_summary(er: ExecutionResult, legacy_summary: str) -> str:
        lines: list[str] = []

        # Title with the HUMAN-READABLE workspace name.
        lines.append(f"**Workspace:** {er.workspace_display_name}")

        if er.template_name:
            lines.append(f"**Template:** {er.template_name}")
        if er.strategy:
            lines.append(f"**Strategy:** {er.strategy}")

        if er.created_files:
            lines.append("**Files created:**")
            lines.extend(f"- {p}" for p in er.created_files)
        if er.modified_files:
            lines.append("**Files modified:**")
            lines.extend(f"- {p}" for p in er.modified_files)
        if er.deleted_files:
            lines.append("**Files deleted:**")
            lines.extend(f"- {p}" for p in er.deleted_files)

        if er.tests:
            passed = er.tests.get("passed")
            failed = er.tests.get("failed")
            if passed is not None or failed is not None:
                lines.append(f"**Tests:** passed={passed or 0}, failed={failed or 0}")
            elif er.tests.get("summary"):
                lines.append(f"**Tests:** {er.tests['summary']}")

        if er.deployment and er.deployment.get("url"):
            lines.append(f"**Deployment:** {er.deployment['url']}")

        if er.warnings:
            lines.append(f"**Warnings:** {', '.join(er.warnings)}")
        elif er.status == "completed":
            lines.append("**Warnings:** None")

        if er.status == "completed":
            lines.append("**Status:** Completed successfully.")
        else:
            err_text = "; ".join(er.errors or []) or "execution failed"
            lines.append(f"**Status:** Failed — {err_text}")

        # If a legacy human-written summary exists, append it (non-destructive).
        body = "\n".join(lines)
        if legacy_summary and legacy_summary not in body:
            body = f"{body}\n\n{legacy_summary}"
        return body

    @staticmethod
    def build_conversation_summary(er: ExecutionResult) -> str:
        """Structured completion record for chat history (future turns)."""
        parts: list[str] = []
        parts.append(f"Workspace: {er.workspace_display_name}")
        if er.template_name:
            parts.append(f"Template: {er.template_name}")
        if er.strategy:
            parts.append(f"Strategy: {er.strategy}")
        if er.created_files:
            parts.append("Files: " + ", ".join(er.created_files))
        elif er.modified_files:
            parts.append("Files: " + ", ".join(er.modified_files))
        parts.append(f"Status: {er.status}")
        if er.deployment and er.deployment.get("url"):
            parts.append(f"Deployment: {er.deployment['url']}")
        if er.warnings:
            parts.append(f"Warnings: {', '.join(er.warnings)}")
        return "\n".join(parts)

    @staticmethod
    def build_context_state(er: ExecutionResult) -> dict[str, Any]:
        """Last-execution state for ContextEngine (no re-discovery needed)."""
        return {
            "workspace_display_name": er.workspace_display_name,
            "workspace_id": er.workspace_id,
            "last_template": er.template_name,
            "last_strategy": er.strategy,
            "last_execution_summary": er.summary,
            "last_deployment_url": (er.deployment or {}).get("url"),
            "last_warnings": er.warnings,
            "last_files": er.created_files or er.modified_files,
            "last_status": er.status,
        }
