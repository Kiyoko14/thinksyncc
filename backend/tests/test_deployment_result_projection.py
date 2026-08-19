from __future__ import annotations

from datetime import datetime, timezone

from models.agent import AgentStep, StepResult, ToolName
from models.job import JobResponse, JobStatus
from services.agent_service import to_forge_v2_response
from services.execution_result_projector import ExecutionResultProjector


def _step() -> dict:
    return {
        "step": 1,
        "tool": ToolName.RUN_COMMAND.value,
        "args": {"command": "echo ok"},
        "stdout": "ok",
        "stderr": "",
        "exit_code": 0,
        "duration_ms": 1,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "success": True,
    }


def test_projector_does_not_invent_deployment_from_workspace_url() -> None:
    projected = ExecutionResultProjector.project(
        {"success": True},
        workspace={"id": "ws-1", "name": "TaskFlow", "slug": "taskflow"},
        objective="Create a landing page",
        workspace_url="https://taskflow.example.com",
    )

    assert projected.deployment is None
    assert "Deployment:" not in projected.summary


def test_projector_preserves_explicit_verified_deployment() -> None:
    projected = ExecutionResultProjector.project(
        {"success": True, "deployment": {"url": "https://taskflow.example.com", "verified": True}},
        workspace={"id": "ws-1", "name": "TaskFlow", "slug": "taskflow"},
        objective="Create a landing page",
    )

    assert projected.deployment == {"url": "https://taskflow.example.com", "verified": True}
    assert "**Deployment:** https://taskflow.example.com" in projected.summary


def test_forge_v2_response_surfaces_verified_deployment_from_summary() -> None:
    job = JobResponse(
        id="job-1",
        user_id="user-1",
        workspace_id="ws-1",
        server_id="srv-1",
        objective="Create a landing page",
        status=JobStatus.COMPLETED,
        allow_write=True,
        dry_run=False,
        intent="server",
        task_mode="complex",
        plan=[],
        steps=[],
        decisions=[],
        errors=[],
        retries=[],
        summary="**Workspace:** TaskFlow\n**Deployment:** https://taskflow.example.com\n**Status:** Completed successfully.",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    response = to_forge_v2_response(job)

    assert response["run"] is not None
    assert response["run"]["deployment"] == {"url": "https://taskflow.example.com", "verified": True}
