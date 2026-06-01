"""ExecutionRepository: durable, normalized storage for job execution metadata.

Dual-write strategy:
- Writes new records to dedicated tables (job_steps, job_decisions, job_retries, job_execution_details)
- Also keeps existing JSONB columns on the jobs table for backward compatibility
- Reads from dedicated tables for full audit trail; JSONB is a denormalized cache
"""

from __future__ import annotations

import logging
from typing import Any

from core.database import get_supabase
from models.agent import AgentDecision, StepResult

logger = logging.getLogger(__name__)

# =============================================================================
# 1. Step Records
# =============================================================================


def save_step(
    job_id: str,
    result: StepResult,
) -> bool:
    """Persist a single execution step to the job_steps table.

    Returns True on success. Logs warning on failure but never raises.
    """
    try:
        record = {
            "job_id": job_id,
            "step_number": result.step,
            "tool": result.tool.value if result.tool else "",
            "args": result.args or {},
            "command": (result.command or "")[:2000],
            "command_type": result.command_type or "",
            "stdout": (result.stdout or "")[:6000],
            "stderr": (result.stderr or "")[:3000],
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "success": result.success,
            "validation_passed": result.validation_passed,
            "status": result.status or "",
            "agent_reasoning": result.agent_reasoning or "",
            "executed_at": result.executed_at.isoformat() if result.executed_at else None,
        }
        get_supabase().table("job_steps").insert(record).execute()
        return True
    except Exception as exc:
        logger.warning("ExecutionRepository.save_step failed (job=%s, step=%s): %s", job_id, result.step, exc)
        return False


def save_steps(
    job_id: str,
    steps: list[StepResult] | list[dict[str, Any]],
) -> bool:
    """Bulk-save steps to the job_steps table.

    Also updates the jobs.steps JSONB column as a backward-compatible cache.
    """
    try:
        records: list[dict[str, Any]] = []
        for s in steps:
            if isinstance(s, StepResult):
                records.append(
                    {
                        "job_id": job_id,
                        "step_number": s.step,
                        "tool": s.tool.value if s.tool else "",
                        "args": s.args or {},
                        "command": (s.command or "")[:2000],
                        "command_type": s.command_type or "",
                        "stdout": (s.stdout or "")[:6000],
                        "stderr": (s.stderr or "")[:3000],
                        "exit_code": s.exit_code,
                        "duration_ms": s.duration_ms,
                        "success": s.success,
                        "validation_passed": s.validation_passed,
                        "status": s.status or "",
                        "agent_reasoning": s.agent_reasoning or "",
                        "executed_at": s.executed_at.isoformat() if s.executed_at else None,
                    }
                )
            else:
                record = dict(s)
                record["job_id"] = job_id
                record["stdout"] = str(record.get("stdout", ""))[:6000]
                record["stderr"] = str(record.get("stderr", ""))[:3000]
                record["command"] = str(record.get("command", ""))[:2000]
                records.append(record)

        if records:
            get_supabase().table("job_steps").insert(records).execute()

        # Update JSONB cache
        step_dicts = [s.model_dump(mode="json") if isinstance(s, StepResult) else dict(s) for s in steps]
        get_supabase().table("jobs").update({"steps": step_dicts}).eq("id", job_id).execute()
        return True
    except Exception as exc:
        logger.warning("ExecutionRepository.save_steps failed (job=%s): %s", job_id, exc)
        return False


def get_steps(job_id: str) -> list[dict[str, Any]]:
    """Retrieve all steps for a job from the normalized table."""
    try:
        result = (
            get_supabase()
            .table("job_steps")
            .select("*")
            .eq("job_id", job_id)
            .order("step_number", desc=False)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("ExecutionRepository.get_steps failed (job=%s): %s", job_id, exc)
        return []


# =============================================================================
# 2. Decision Records
# =============================================================================


def save_decision(
    job_id: str,
    decision: AgentDecision,
    step_number: int | None = None,
) -> bool:
    """Persist a decision to the job_decisions table."""
    try:
        record = {
            "job_id": job_id,
            "step_number": step_number,
            "action": decision.action.value if decision.action else "",
            "reason": decision.reason or "",
            "summary_so_far": decision.summary_so_far or "",
            "modified_step": decision.modified_step.model_dump(mode="json") if decision.modified_step else None,
        }
        get_supabase().table("job_decisions").insert(record).execute()
        return True
    except Exception as exc:
        logger.warning("ExecutionRepository.save_decision failed (job=%s): %s", job_id, exc)
        return False


def save_decisions(
    job_id: str,
    decisions: list[AgentDecision] | list[dict[str, Any]],
) -> bool:
    """Bulk-save decisions to the job_decisions table and update JSONB cache."""
    try:
        records: list[dict[str, Any]] = []
        for d in decisions:
            if isinstance(d, AgentDecision):
                records.append(
                    {
                        "job_id": job_id,
                        "action": d.action.value if d.action else "",
                        "reason": d.reason or "",
                        "summary_so_far": d.summary_so_far or "",
                        "modified_step": d.modified_step.model_dump(mode="json") if d.modified_step else None,
                    }
                )
            else:
                record = dict(d)
                record["job_id"] = job_id
                records.append(record)

        if records:
            get_supabase().table("job_decisions").insert(records).execute()

        decision_dicts = [d.model_dump(mode="json") if isinstance(d, AgentDecision) else dict(d) for d in decisions]
        get_supabase().table("jobs").update({"decisions": decision_dicts}).eq("id", job_id).execute()
        return True
    except Exception as exc:
        logger.warning("ExecutionRepository.save_decisions failed (job=%s): %s", job_id, exc)
        return False


def get_decisions(job_id: str) -> list[dict[str, Any]]:
    """Retrieve all decisions for a job from the normalized table."""
    try:
        result = (
            get_supabase()
            .table("job_decisions")
            .select("*")
            .eq("job_id", job_id)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("ExecutionRepository.get_decisions failed (job=%s): %s", job_id, exc)
        return []


# =============================================================================
# 3. Retry Records
# =============================================================================


def save_retry(
    job_id: str,
    step_number: int,
    attempt: int,
    command: str | None,
    command_type: str | None,
    reason: str | None,
) -> bool:
    """Persist a retry to the job_retries table."""
    try:
        record = {
            "job_id": job_id,
            "step_number": step_number,
            "attempt": attempt,
            "command": command or "",
            "command_type": command_type or "",
            "reason": reason or "",
        }
        get_supabase().table("job_retries").insert(record).execute()
        return True
    except Exception as exc:
        logger.warning("ExecutionRepository.save_retry failed (job=%s): %s", job_id, exc)
        return False


def save_retries(
    job_id: str,
    retries: list[dict[str, Any]],
) -> bool:
    """Bulk-save retries to the job_retries table and update JSONB cache."""
    try:
        records = [dict(r) for r in retries]
        for r in records:
            r["job_id"] = job_id

        if records:
            get_supabase().table("job_retries").insert(records).execute()

        get_supabase().table("jobs").update({"retries": retries}).eq("id", job_id).execute()
        return True
    except Exception as exc:
        logger.warning("ExecutionRepository.save_retries failed (job=%s): %s", job_id, exc)
        return False


def get_retries(job_id: str) -> list[dict[str, Any]]:
    """Retrieve all retries for a job from the normalized table."""
    try:
        result = (
            get_supabase()
            .table("job_retries")
            .select("*")
            .eq("job_id", job_id)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("ExecutionRepository.get_retries failed (job=%s): %s", job_id, exc)
        return []


# =============================================================================
# 4. Execution Details (errors, metadata, analysis)
# =============================================================================


def save_execution_detail(
    job_id: str,
    detail_type: str,
    payload: dict[str, Any],
    step_number: int | None = None,
) -> bool:
    """Persist a detail record to the job_execution_details table.

    detail_type must be one of: error, metadata, analysis, contract.
    """
    try:
        record = {
            "job_id": job_id,
            "detail_type": detail_type,
            "step_number": step_number,
            "payload": payload,
        }
        get_supabase().table("job_execution_details").insert(record).execute()
        return True
    except Exception as exc:
        logger.warning("ExecutionRepository.save_execution_detail failed (job=%s): %s", job_id, exc)
        return False


def save_errors(
    job_id: str,
    errors: list[dict[str, Any]],
) -> bool:
    """Bulk-save errors as execution_details records and update JSONB cache."""
    try:
        records = []
        for e in errors:
            records.append(
                {
                    "job_id": job_id,
                    "detail_type": "error",
                    "step_number": e.get("step"),
                    "payload": e,
                }
            )
        if records:
            get_supabase().table("job_execution_details").insert(records).execute()

        get_supabase().table("jobs").update({"errors": errors}).eq("id", job_id).execute()
        return True
    except Exception as exc:
        logger.warning("ExecutionRepository.save_errors failed (job=%s): %s", job_id, exc)
        return False


def get_execution_details(
    job_id: str,
    detail_type: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve execution details for a job, optionally filtered by type."""
    try:
        query = (
            get_supabase()
            .table("job_execution_details")
            .select("*")
            .eq("job_id", job_id)
        )
        if detail_type:
            query = query.eq("detail_type", detail_type)
        result = query.order("created_at", desc=False).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("ExecutionRepository.get_execution_details failed (job=%s): %s", job_id, exc)
        return []


# =============================================================================
# 5. Reconstruction
# =============================================================================


def reconstruct_job_execution(job_id: str) -> dict[str, Any]:
    """Reconstruct a complete job execution from normalized tables.

    Returns dict with steps, decisions, retries, errors, and metadata.
    """
    return {
        "job_id": job_id,
        "steps": get_steps(job_id),
        "decisions": get_decisions(job_id),
        "retries": get_retries(job_id),
        "errors": get_execution_details(job_id, detail_type="error"),
        "metadata": get_execution_details(job_id, detail_type="metadata"),
        "analysis": get_execution_details(job_id, detail_type="analysis"),
        "contracts": get_execution_details(job_id, detail_type="contract"),
    }
