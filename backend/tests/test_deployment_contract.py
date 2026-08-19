"""Tests for the 5-stage deployment verification contract — BUG #3.

The executor must run the full contract:
  Stage 1: port listening  (ss -tulnp | grep :{port})
  Stage 2: local curl      (curl -f http://127.0.0.1:{port})
  Stage 3: gateway route   (host-header curl — optional)
  Stage 4: public URL      (curl -f {protocol}://{subdomain} — optional)

And must NEVER set success=True before any verification runs.
"""
from __future__ import annotations

import sys
import unittest
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ["REDIS_URL"] = "redis://localhost:6379"

from models.agent import AgentStep, StepResult, ToolName
from services.capability_service import WorkspaceContext
from services.deploy_service import DeployService
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


class DeploymentContractTests(unittest.IsolatedAsyncioTestCase):
    def test_workspace_domain_must_be_safe_subdomain(self) -> None:
        self.assertEqual(
            DeployService._require_workspace_domain("demo.thinksync.art", "thinksync.art"),
            "demo.thinksync.art",
        )
        for value in (
            "attacker.example",
            "demo.thinksync.art; return 0",
            "demo\nserver_name bad",
            "thinksync.art",
        ):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    DeployService._require_workspace_domain(value, "thinksync.art")

    async def test_success_requires_port_listening_and_curl(self) -> None:
        """Deployment must not succeed unless port-listen + curl both pass."""
        commands_called: list[str] = []

        async def fake_execute(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            cmd = str(args.get("command") or "")
            commands_called.append(cmd)
            # Port listen: report listening.
            if cmd.startswith("ss") and "4000" in cmd:
                return _result(step=int(kwargs["step_number"]), args=args,
                               exit_code=0, stdout="tcp LISTEN 0 128 *:4000")
            # Local curl: succeed.
            if "curl" in cmd and "127.0.0.1:4000" in cmd:
                return _result(step=int(kwargs["step_number"]), args=args, exit_code=0, stdout="OK")
            # Everything else (plan step).
            return _result(step=int(kwargs["step_number"]), args=args, exit_code=0, stdout="started")

        step = AgentStep(
            step=1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": "nohup python3 -m http.server 4000 --bind 127.0.0.1 > app.log 2>&1 &"},
            reason="Start server.",
        )
        ctx = WorkspaceContext(workspace_id="ws-1", port=4000, subdomain="myapp-abc123")

        with (
            patch("services.capability_service.detect_capabilities", return_value={"python": True}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute),
            patch("services.capability_service.load_workspace_context", new_callable=AsyncMock),
        ):
            result = await run_server_execution(
                objective="deploy app",
                intent="server",
                task_mode="complex",
                plan_steps=[step],
                server={},
                workspace_id="ws-1",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=1,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
                workspace_context=ctx,
            )

        self.assertTrue(result.success)
        self.assertTrue(any("ss" in c and "4000" in c for c in commands_called),
                        "Stage 1 (port-listen check) was not executed")
        self.assertTrue(any("curl" in c and "127.0.0.1:4000" in c for c in commands_called),
                        "Stage 2 (local curl) was not executed")

    async def test_failure_when_port_not_listening_and_no_python(self) -> None:
        """If port is not listening and python unavailable, result must be FAILED."""
        async def fake_execute(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            cmd = str(args.get("command") or "")
            # Port listen: not found.
            if cmd.startswith("ss"):
                return _result(step=int(kwargs["step_number"]), args=args,
                               exit_code=1, stdout="")
            return _result(step=int(kwargs["step_number"]), args=args, exit_code=0)

        step = AgentStep(
            step=1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": "nohup node server.js > app.log 2>&1 &"},
            reason="Start server.",
        )
        ctx = WorkspaceContext(workspace_id="ws-2", port=4000, subdomain="myapp-xyz789")

        with (
            patch("services.capability_service.detect_capabilities",
                  return_value={"python": False, "npm": True, "pm2": False}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute),
            patch("services.capability_service.load_workspace_context", new_callable=AsyncMock),
            patch("services.executor.asyncio.sleep", return_value=None),
        ):
            result = await run_server_execution(
                objective="deploy app",
                intent="server",
                task_mode="complex",
                plan_steps=[step],
                server={},
                workspace_id="ws-2",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=1,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
                workspace_context=ctx,
            )

        self.assertFalse(result.success)

    async def test_no_premature_success_before_verification(self) -> None:
        """Port not listening + fallback unavailable → success must be False.

        Before BUG #3 fix, the premature ``success = True`` line caused the
        executor to return success=True immediately after plan steps ran, even
        when no server was actually listening.  After the fix, the deployment
        contract gate must produce success=False when it cannot verify.
        """
        async def fake_execute(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            cmd = str(args.get("command") or "")
            # Port listen check: port is NOT listening.
            if cmd.startswith("ss"):
                return _result(step=int(kwargs["step_number"]), args=args,
                               exit_code=1, stdout="")
            # Everything else (plan steps, preflight): succeed.
            return _result(step=int(kwargs["step_number"]), args=args,
                           exit_code=0, stdout="ok")

        step = AgentStep(
            step=1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": "pm2 start server.js --name myapp"},
            reason="Start server with PM2.",
        )
        ctx = WorkspaceContext(workspace_id="ws-3", port=4000, subdomain="myapp-abc123")

        with (
            # python=False → fallback server cannot start → no escape hatch.
            patch("services.capability_service.detect_capabilities",
                  return_value={"python": False, "npm": True, "pm2": True}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute),
            patch("services.capability_service.load_workspace_context", new_callable=AsyncMock),
        ):
            result = await run_server_execution(
                objective="deploy app",
                intent="server",
                task_mode="complex",
                plan_steps=[step],
                server={},
                workspace_id="ws-3",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=1,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
                workspace_context=ctx,
            )

        # Port not listening + no python fallback → deployment contract fails.
        # success=True must NEVER be returned without passing all contract stages.
        self.assertFalse(result.success,
                         "success=True must never be set before verification passes")

    async def test_public_url_in_summary_not_localhost(self) -> None:
        """Summary URL must be the platform subdomain URL, not http://127.0.0.1:PORT."""
        async def fake_execute(**kwargs: object) -> StepResult:
            args = kwargs["args"]
            assert isinstance(args, dict)
            cmd = str(args.get("command") or "")
            if cmd.startswith("ss") and "5555" in cmd:
                return _result(step=int(kwargs["step_number"]), args=args,
                               exit_code=0, stdout="tcp LISTEN 0 128 *:5555")
            if "curl" in cmd and "127.0.0.1:5555" in cmd:
                return _result(step=int(kwargs["step_number"]), args=args,
                               exit_code=0, stdout="OK")
            return _result(step=int(kwargs["step_number"]), args=args, exit_code=0, stdout="ok")

        step = AgentStep(
            step=1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": "nohup python3 -m http.server 5555 > app.log 2>&1 &"},
            reason="Start server.",
        )
        ctx = WorkspaceContext(
            workspace_id="ws-4",
            port=5555,
            subdomain="testapp-def456",
            protocol="https",
        )

        with (
            patch("services.capability_service.detect_capabilities", return_value={"python": True}),
            patch("services.redis_service.RedisService.get_sync_client", return_value=_RedisStub()),
            patch("services.executor.execute_tool", side_effect=fake_execute),
            patch("services.capability_service.load_workspace_context", new_callable=AsyncMock),
        ):
            result = await run_server_execution(
                objective="deploy app",
                intent="server",
                task_mode="complex",
                plan_steps=[step],
                server={},
                workspace_id="ws-4",
                workspace_path="/root/workspaces/test",
                allow_write=True,
                max_steps=1,
                step_timeout=5,
                config=ExecutionConfig(max_heal_attempts=0),
                workspace_context=ctx,
            )

        self.assertTrue(result.success)
        self.assertNotIn("127.0.0.1", result.summary,
                         "Summary must not expose 127.0.0.1 as the public URL")
        self.assertIn("testapp-def456", result.summary,
                      "Summary must include the public subdomain URL")


if __name__ == "__main__":
    unittest.main()
