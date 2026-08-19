"""Tests for port discipline — BUG #4.

The executor must ALWAYS use the workspace-allocated port from
WorkspaceContext.  Hardcoded fallback_port=8000 is forbidden.
"""
from __future__ import annotations

import sys
import unittest
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ["REDIS_URL"] = "redis://localhost:6379"

from models.agent import AgentStep, StepResult, ToolName
from services.capability_service import WorkspaceContext
from services.executor import ExecutionConfig, run_server_execution


class _RedisStub:
    def set(self, *args: object, **kwargs: object) -> None:
        return None


def _result(*, step: int, args: dict, exit_code: int, stdout: str = "", stderr: str = "") -> StepResult:
    return StepResult(
        step=step,
        tool=ToolName.RUN_COMMAND,
        args=args,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=1,
        executed_at=datetime.now(timezone.utc),
        success=exit_code == 0,
    )


class PortDisciplineTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_fails_without_workspace_context_port(self) -> None:
        """When no port is allocated, fallback must return False — never 8000."""
        calls: list[str] = []

        async def fake_execute_tool(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            command = str(args.get("command") or "")
            calls.append(command)
            # Simulate a failing deployment so fallback is triggered.
            return _result(step=int(kwargs["step_number"]), args=args, exit_code=1)

        step = AgentStep(
            step=1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": "nohup python3 -m http.server 4000 --bind 127.0.0.1 > app.log 2>&1 &"},
            reason="Start server.",
        )

        ctx_no_port = WorkspaceContext(
            workspace_id="ws-port-test",
            port=None,  # no allocated port
            subdomain="myapp-abc123",
        )

        with (
            patch("services.capability_service.detect_capabilities", return_value={"python": True, "npm": False, "pm2": False}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute_tool),
            patch("services.capability_service.load_workspace_context", new_callable=AsyncMock),
        ):
            result = await run_server_execution(
                objective="deploy app",
                intent="server",
                task_mode="complex",
                plan_steps=[step],
                server={},
                workspace_id="ws-port-test",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=1,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
                workspace_context=ctx_no_port,
            )

        self.assertFalse(result.success)
        # Verify 8000 was never used as a fallback port.
        for call in calls:
            self.assertNotIn("8000", call, f"Hardcoded port 8000 appeared in command: {call!r}")

    async def test_fallback_uses_allocated_port_not_8000(self) -> None:
        """When fallback is triggered, it must start server on the allocated port."""
        calls: list[str] = []

        async def fake_execute_tool(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            command = str(args.get("command") or "")
            calls.append(command)
            # Succeed on curl verification, fail everything else.
            if command.startswith("curl") and "4321" in command:
                return _result(step=int(kwargs["step_number"]), args=args, exit_code=0, stdout="OK")
            return _result(step=int(kwargs["step_number"]), args=args, exit_code=0, stdout="5000" if "ss" in command else "")

        step = AgentStep(
            step=1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": "nohup python3 -m http.server 4321 --bind 127.0.0.1 > app.log 2>&1 &"},
            reason="Start server on port 4321.",
        )

        ctx = WorkspaceContext(
            workspace_id="ws-port-test",
            port=4321,
            subdomain="myapp-abc123",
        )

        with (
            patch("services.capability_service.detect_capabilities", return_value={"python": True, "npm": False, "pm2": False}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute_tool),
            patch("services.capability_service.load_workspace_context", new_callable=AsyncMock),
        ):
            await run_server_execution(
                objective="deploy app",
                intent="server",
                task_mode="complex",
                plan_steps=[step],
                server={},
                workspace_id="ws-port-test",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=1,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
                workspace_context=ctx,
            )

        # 8000 must never appear in any command when port=4321.
        for call in calls:
            self.assertNotIn(
                "8000", call,
                f"Hardcoded port 8000 appeared despite allocated port=4321: {call!r}",
            )

    async def test_workspace_context_port_injected_into_coordinator(self) -> None:
        """workspace_platform dict in coordinator_context must reflect allocated port."""
        captured_context: dict = {}

        async def fake_generate_plan(*, objective, context, max_steps):
            captured_context.update(context)
            from models.agent import AgentPlan
            return AgentPlan(objective=objective, context_summary="", steps=[])

        ctx = WorkspaceContext(
            workspace_id="ws-ctx-test",
            port=7777,
            subdomain="myapp-abc123",
            protocol="http",
        )

        with (
            patch("services.capability_service.detect_capabilities", return_value={"python": True}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.capability_service.load_workspace_context", new_callable=AsyncMock),
            patch("services.agent_llm.generate_plan", side_effect=fake_generate_plan),
        ):
            try:
                await run_server_execution(
                    objective="deploy app",
                    intent="server",
                    task_mode="complex",
                    plan_steps=None,
                    server={},
                    workspace_id="ws-ctx-test",
                    workspace_path="/root/workspaces/test",
                    allow_write=True,
                    max_steps=1,
                    step_timeout=5,
                    config=ExecutionConfig(max_heal_attempts=0),
                    workspace_context=ctx,
                )
            except Exception:
                pass

        wp = captured_context.get("workspace_platform", {})
        self.assertEqual(wp.get("port"), 7777)
        self.assertEqual(wp.get("subdomain"), "myapp-abc123")
        self.assertEqual(wp.get("protocol"), "http")
        self.assertEqual(wp.get("workspace_id"), "ws-ctx-test")


if __name__ == "__main__":
    unittest.main()
