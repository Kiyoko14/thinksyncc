from __future__ import annotations

import sys
import unittest
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ["REDIS_URL"] = "redis://localhost:6379"

from models.agent import AgentStep, StepResult, ToolName
from services.executor import ExecutionConfig, run_server_execution
from services.tools import classify_command, command_success


class _RedisStub:
    def set(self, *args: object, **kwargs: object) -> None:
        return None


def _result(*, step: int, args: dict[str, object], exit_code: int, stdout: str = "", stderr: str = "") -> StepResult:
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


class ExecutorValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_check_exit_one_is_condition_false_not_error(self) -> None:
        command_type = classify_command("ss -tulnp | grep 8081")

        self.assertEqual(command_type, "CHECK")
        self.assertTrue(command_success(command_type, 0))
        self.assertTrue(command_success(command_type, 1))
        self.assertFalse(command_success(command_type, 2))

    async def test_check_command_is_not_retried(self) -> None:
        calls: list[str] = []

        async def fake_execute_tool(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            command = str(args.get("command") or "")
            calls.append(command)
            return _result(step=int(kwargs["step_number"]), args=args, exit_code=1)

        step = AgentStep(
            step=1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": "ss -tulnp | grep 8081"},
            reason="Check whether port 8081 is listening.",
        )

        with (
            patch("services.capability_service.detect_capabilities", return_value={"python": True, "npm": True, "pm2": True}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute_tool),
        ):
            result = await run_server_execution(
                objective="inspect port",
                intent="server",
                task_mode="complex",
                plan_steps=[step],
                server={},
                workspace_id="workspace-1",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=1,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
            )

        self.assertTrue(result.success)
        self.assertEqual(calls, ["ss -tulnp | grep 8081"])
        self.assertEqual(result.retries, [])
        self.assertEqual(result.steps[0].command_type, "CHECK")
        self.assertEqual(result.steps[0].status, "validated")

    async def test_action_retries_twice_after_failed_validation(self) -> None:
        calls: list[str] = []

        async def fake_execute_tool(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            command = str(args.get("command") or "")
            calls.append(command)
            exit_code = 1 if command == "test -d out" else 0
            return _result(step=int(kwargs["step_number"]), args=args, exit_code=exit_code)

        step = AgentStep(
            step=1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": "mkdir -p out"},
            reason="Create output directory.",
        )

        with (
            patch("services.capability_service.detect_capabilities", return_value={"python": True, "npm": True, "pm2": True}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute_tool),
            patch("services.executor.asyncio.sleep", return_value=None),
        ):
            result = await run_server_execution(
                objective="create directory",
                intent="server",
                task_mode="complex",
                plan_steps=[step],
                server={},
                workspace_id="workspace-1",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=1,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
            )

        self.assertFalse(result.success)
        self.assertEqual(calls, ["mkdir -p out", "test -d out", "mkdir -p out", "test -d out", "mkdir -p out", "test -d out"])
        self.assertEqual(len(result.retries), 2)
        self.assertTrue(all(item["command_type"] == "ACTION" for item in result.retries))
        self.assertEqual([step.status for step in result.steps], ["failed", "failed", "failed"])

    async def test_next_step_waits_until_prior_step_validator_passes(self) -> None:
        calls: list[str] = []

        async def fake_execute_tool(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            command = str(args.get("command") or "")
            calls.append(command)
            return _result(step=int(kwargs["step_number"]), args=args, exit_code=0)

        steps = [
            AgentStep(
                step=1,
                tool=ToolName.RUN_COMMAND.value,
                args={"command": "nohup python3 -m http.server 8081 --bind 127.0.0.1 > app.log 2>&1 &"},
                reason="Start local server.",
            ),
            AgentStep(
                step=2,
                tool=ToolName.RUN_COMMAND.value,
                args={"command": "curl -f http://127.0.0.1:8081"},
                reason="Verify local server response.",
            ),
        ]

        with (
            patch("services.capability_service.detect_capabilities", return_value={"python": True, "npm": True, "pm2": True}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute_tool),
        ):
            result = await run_server_execution(
                objective="serve on port 8081",
                intent="server",
                task_mode="complex",
                plan_steps=steps,
                server={},
                workspace_id="workspace-1",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=2,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
            )

        self.assertTrue(result.success)
        self.assertEqual(
            calls,
            [
                "nohup python3 -m http.server 8081 --bind 127.0.0.1 > app.log 2>&1 &",
                "ss -tulnp | grep :8081",
                "curl -f http://127.0.0.1:8081",
            ],
        )
        self.assertEqual([step.status for step in result.steps], ["validated", "validated"])


if __name__ == "__main__":
    unittest.main()
