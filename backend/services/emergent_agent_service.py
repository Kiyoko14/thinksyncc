import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from models.agent import (
    AgentExecutionResult,
    AgentJobResponse,
    AgentJobStatus,
    AgentOrchestrationRequest,
    AgentOrchestrationResponse,
    AgentPlanResponse,
    AgentPlanStep,
    AgentRunRequest,
    AgentRunResponse,
    ArchitectureHistoryItem,
)
from services.chat_service import ChatService
from services.server_service import ServerService
from services.ssh_service import SSHService
from services.workspace_service import WorkspaceService


class EmergentE1Service:
    """Professional operations agent with strict command safety controls."""

    _BLOCKED_PATTERNS = [
        re.compile(r"\brm\s+-rf\b", flags=re.IGNORECASE),
        re.compile(r"\bmkfs\b", flags=re.IGNORECASE),
        re.compile(r"\bdd\s+if=", flags=re.IGNORECASE),
        re.compile(r"\bshutdown\b", flags=re.IGNORECASE),
        re.compile(r"\breboot\b", flags=re.IGNORECASE),
        re.compile(r"\bpasswd\b", flags=re.IGNORECASE),
    ]

    _READ_ONLY_PREFIXES = (
        "uname",
        "uptime",
        "whoami",
        "id",
        "pwd",
        "ls",
        "df",
        "free",
        "cat",
        "head",
        "tail",
        "ps",
        "ss",
        "netstat",
        "docker ps",
        "docker images",
        "systemctl status",
        "journalctl",
    )

    # In-memory async job store. Keyed by job_id (UUID string).
    _jobs: dict[str, dict] = {}

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [item.strip().lower() for item in value.split(",") if item.strip()]

    @staticmethod
    def _is_write_allowed(user_email: str | None) -> bool:
        settings = get_settings()
        admins = set(EmergentE1Service._split_csv(settings.AGENT_ADMIN_EMAILS))
        if not admins:
            return False
        return (user_email or "").strip().lower() in admins

    @staticmethod
    def _allowed_prefixes() -> tuple[str, ...]:
        settings = get_settings()
        prefixes = EmergentE1Service._split_csv(settings.AGENT_ALLOWED_COMMAND_PREFIXES)
        if not prefixes:
            return EmergentE1Service._READ_ONLY_PREFIXES
        return tuple(prefixes)

    @staticmethod
    def _is_in_allowlist(command: str) -> bool:
        lowered = command.strip().lower()
        return any(lowered.startswith(prefix) for prefix in EmergentE1Service._allowed_prefixes())

    @staticmethod
    def _is_safe_command(command: str, allow_write: bool) -> bool:
        normalized = command.strip()
        if not normalized:
            return False

        for pattern in EmergentE1Service._BLOCKED_PATTERNS:
            if pattern.search(normalized):
                return False

        if not EmergentE1Service._is_in_allowlist(normalized):
            return False

        if allow_write:
            return True

        lowered = normalized.lower()
        return any(lowered.startswith(prefix) for prefix in EmergentE1Service._READ_ONLY_PREFIXES)

    @staticmethod
    def _build_plan(objective: str, max_steps: int, allow_write: bool) -> list[AgentPlanStep]:
        lowered = objective.lower()
        commands: list[tuple[str, str]] = []

        if "health" in lowered or "audit" in lowered or "monitor" in lowered:
            commands.extend(
                [
                    ("uptime", "Server uptime va load holatini tekshirish"),
                    ("df -h", "Disk sig'im va to'lish holatini tekshirish"),
                    ("free -m", "RAM holatini tekshirish"),
                ]
            )

        if "docker" in lowered or "container" in lowered:
            commands.extend(
                [
                    ("docker ps", "Ishlayotgan containerlarni ko'rish"),
                    ("docker images | head -n 20", "Asosiy image'larni ko'rish"),
                ]
            )

        if "log" in lowered:
            commands.append(("journalctl -n 100 --no-pager", "Oxirgi log yozuvlarini tahlil qilish"))

        if not commands:
            commands = [
                ("uname -a", "Server platformasi va kernel versiyasini aniqlash"),
                ("uptime", "Umumiy ishlash holatini ko'rish"),
                ("df -h", "Disk holatini ko'rish"),
            ]

        plan: list[AgentPlanStep] = []
        for index, (command, rationale) in enumerate(commands[:max_steps], start=1):
            approved = EmergentE1Service._is_safe_command(command=command, allow_write=allow_write)
            plan.append(
                AgentPlanStep(
                    step=index,
                    command=command,
                    rationale=rationale,
                    approved=approved,
                )
            )
        return plan

    @staticmethod
    async def _execute_plan_step(
        step: AgentPlanStep,
        server: dict[str, Any],
        semaphore: asyncio.Semaphore,
        step_timeout_seconds: int,
    ) -> tuple[int, AgentExecutionResult]:
        async with semaphore:
            response = await SSHService.execute(
                server=server,
                command=step.command,
                command_timeout=step_timeout_seconds,
            )
            return (
                step.step,
                AgentExecutionResult(
                    command=step.command,
                    output=response.output,
                    exit_code=response.exit_code,
                    executed_at=response.executed_at,
                ),
            )

    @staticmethod
    def _audit_log(
        *,
        user_id: str,
        user_email: str,
        payload: AgentRunRequest,
        plan: list[AgentPlanStep],
        results: list[AgentExecutionResult],
        summary: str,
        success: bool,
    ) -> None:
        settings = get_settings()
        if not settings.AGENT_AUDIT_LOGGING_ENABLED:
            return

        supabase = get_supabase()
        record = {
            "user_id": user_id,
            "user_email": user_email,
            "server_id": payload.server_id,
            "objective": payload.objective,
            "dry_run": payload.dry_run,
            "allow_write": payload.allow_write,
            "max_steps": payload.max_steps,
            "plan": [step.model_dump() for step in plan],
            "results": [result.model_dump(mode="json") for result in results],
            "summary": summary,
            "success": success,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            supabase.table(settings.AGENT_AUDIT_TABLE).insert(record).execute()
        except APIError:
            # Audit log failures must not break the user-facing agent run.
            return

    @staticmethod
    async def run(payload: AgentRunRequest, current_user: dict[str, Any]) -> AgentRunResponse:
        settings = get_settings()
        user_id = current_user["sub"]
        user_email = str(current_user.get("email", ""))

        if payload.allow_write and not EmergentE1Service._is_write_allowed(user_email=user_email):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Write-mode is restricted to agent admins",
            )

        server = ServerService.get_server(server_id=payload.server_id, user_id=user_id)
        plan = EmergentE1Service._build_plan(
            objective=payload.objective,
            max_steps=payload.max_steps,
            allow_write=payload.allow_write,
        )

        rejected = [step for step in plan if not step.approved]
        if rejected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Agent plan contains blocked commands. Use a safer objective or enable only approved actions.",
            )

        results: list[AgentExecutionResult] = []
        step_timeout_seconds = payload.step_timeout_seconds or settings.AGENT_STEP_TIMEOUT
        max_concurrency = payload.max_concurrency or settings.AGENT_MAX_CONCURRENCY
        if not payload.dry_run:
            semaphore = asyncio.Semaphore(max_concurrency)
            tasks = [
                EmergentE1Service._execute_plan_step(
                    step=step,
                    server=server,
                    semaphore=semaphore,
                    step_timeout_seconds=step_timeout_seconds,
                )
                for step in plan
            ]
            completed = await asyncio.gather(*tasks)
            results = [result for _, result in sorted(completed, key=lambda item: item[0])]

        ok_count = len([r for r in results if r.exit_code == 0])
        if payload.dry_run:
            summary = f"Dry-run completed: {len(plan)} ta qadam rejalashtirildi, bajarilmadi."
        else:
            summary = f"Execution completed: {len(results)} ta qadam bajarildi, {ok_count} tasi muvaffaqiyatli."

        EmergentE1Service._audit_log(
            user_id=user_id,
            user_email=user_email,
            payload=payload,
            plan=plan,
            results=results,
            summary=summary,
            success=(ok_count == len(results)) if results else payload.dry_run,
        )

        policy = {
            "write_allowed_for_user": EmergentE1Service._is_write_allowed(user_email=user_email),
            "allow_write_requested": payload.allow_write,
            "step_timeout_seconds": step_timeout_seconds,
            "max_concurrency": max_concurrency,
            "audit_logging_enabled": settings.AGENT_AUDIT_LOGGING_ENABLED,
        }

        return AgentRunResponse(
            objective=payload.objective,
            dry_run=payload.dry_run,
            policy=policy,
            plan=plan,
            results=results,
            summary=summary,
        )

    @staticmethod
    def _load_architecture_history(user_id: str, server_id: str, limit: int = 10) -> list[ArchitectureHistoryItem]:
        supabase = get_supabase()
        try:
            result = (
                supabase.table(get_settings().AGENT_AUDIT_TABLE)
                .select("id,objective,summary,success,created_at")
                .eq("user_id", user_id)
                .eq("server_id", server_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
        except APIError:
            return []

        items: list[ArchitectureHistoryItem] = []
        for row in (result.data or []):
            try:
                items.append(ArchitectureHistoryItem(**row))
            except Exception:
                continue
        return items

    @staticmethod
    async def orchestrate(
        payload: AgentOrchestrationRequest,
        current_user: dict[str, Any],
    ) -> AgentOrchestrationResponse:
        user_id = current_user["sub"]

        workspace = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=user_id)
        chat = ChatService.get_chat(workspace_id=payload.workspace_id, user_id=user_id)
        if chat is None:
            chat = ChatService.create_chat(workspace_id=payload.workspace_id, user_id=user_id)

        user_message = ChatService.save_message(
            chat_id=chat["id"],
            role="user",
            content=payload.message,
        )

        run_payload = AgentRunRequest(
            server_id=workspace["server_id"],
            objective=payload.message,
            max_steps=payload.max_steps,
            allow_write=payload.allow_write,
            dry_run=payload.dry_run,
            step_timeout_seconds=payload.step_timeout_seconds,
            max_concurrency=payload.max_concurrency,
        )
        run_response = await EmergentE1Service.run(payload=run_payload, current_user=current_user)

        assistant_text = (
            f"Forge v1 orchestration summary:\n{run_response.summary}\n"
            f"Steps: {len(run_response.plan)} | Results: {len(run_response.results)}"
        )
        assistant_message = ChatService.save_message(
            chat_id=chat["id"],
            role="assistant",
            content=assistant_text,
        )

        history = EmergentE1Service._load_architecture_history(
            user_id=user_id,
            server_id=workspace["server_id"],
        )

        return AgentOrchestrationResponse(
            chat_id=chat["id"],
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            run=run_response,
            architecture_history=history,
        )

    # ------------------------------------------------------------------
    # Async job queue helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_plan(payload: AgentRunRequest, current_user: dict[str, Any]) -> AgentPlanResponse:
        """Return a plan without executing any commands (instant, no SSH)."""
        settings = get_settings()
        user_email = str(current_user.get("email", ""))

        if payload.allow_write and not EmergentE1Service._is_write_allowed(user_email=user_email):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Write-mode is restricted to agent admins",
            )

        plan = EmergentE1Service._build_plan(
            objective=payload.objective,
            max_steps=payload.max_steps,
            allow_write=payload.allow_write,
        )

        step_timeout_seconds = payload.step_timeout_seconds or settings.AGENT_STEP_TIMEOUT
        max_concurrency = payload.max_concurrency or settings.AGENT_MAX_CONCURRENCY

        blocked = [s for s in plan if not s.approved]
        summary = (
            f"Plan ready: {len(plan)} ta qadam. {len(blocked)} ta bloklangan."
            if blocked
            else f"Plan ready: {len(plan)} ta qadam, hammasi tasdiqlangan."
        )

        policy = {
            "write_allowed_for_user": EmergentE1Service._is_write_allowed(user_email=user_email),
            "allow_write_requested": payload.allow_write,
            "step_timeout_seconds": step_timeout_seconds,
            "max_concurrency": max_concurrency,
            "audit_logging_enabled": settings.AGENT_AUDIT_LOGGING_ENABLED,
        }

        return AgentPlanResponse(
            objective=payload.objective,
            policy=policy,
            plan=plan,
            summary=summary,
        )

    @staticmethod
    async def _run_job(job_id: str, payload: AgentRunRequest, current_user: dict[str, Any]) -> None:
        """Background coroutine that executes a job and stores the result."""
        EmergentE1Service._jobs[job_id]["status"] = AgentJobStatus.RUNNING
        try:
            result = await EmergentE1Service.run(payload=payload, current_user=current_user)
            EmergentE1Service._jobs[job_id] = {
                "status": AgentJobStatus.COMPLETED,
                "run": result,
                "error": None,
            }
        except HTTPException as exc:
            EmergentE1Service._jobs[job_id] = {
                "status": AgentJobStatus.FAILED,
                "run": None,
                "error": exc.detail,
            }
        except Exception as exc:
            EmergentE1Service._jobs[job_id] = {
                "status": AgentJobStatus.FAILED,
                "run": None,
                "error": str(exc),
            }

    @staticmethod
    def submit_job(job_id: str) -> None:
        """Register a new job in the in-memory store."""
        EmergentE1Service._jobs[job_id] = {
            "status": AgentJobStatus.QUEUED,
            "run": None,
            "error": None,
        }

    @staticmethod
    def get_job_status(job_id: str) -> AgentJobResponse:
        """Return current status and (when done) result for a job."""
        job = EmergentE1Service._jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )
        return AgentJobResponse(
            job_id=job_id,
            status=job["status"],
            run=job.get("run"),
            error=job.get("error"),
        )
