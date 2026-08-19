"""Tests for WorkspaceContext, PlatformContextError, and load_workspace_context.

BUG #1: capability_service must expose authoritative platform context
        (port, subdomain, SSL, protocol, gateway) — never guess, never hardcode.
"""
from __future__ import annotations

import sys
import unittest
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ["REDIS_URL"] = "redis://localhost:6379"

from services.capability_service import (
    PlatformContextError,
    WorkspaceContext,
    load_workspace_context,
)


class _RedisStub:
    def __init__(self, port: int | None = None, active: bool = False) -> None:
        self._port = port
        self._active = active

    def get(self, key: str) -> str | None:
        if key.endswith(":port") and self._port is not None:
            return str(self._port)
        return None

    def sismember(self, key: str, value: str) -> bool:
        return self._active

    def set(self, *args: object, **kwargs: object) -> None:
        return None


class WorkspaceContextTests(unittest.TestCase):
    def test_base_url_none_when_no_subdomain(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1")
        self.assertIsNone(ctx.base_url)

    def test_base_url_http(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", subdomain="myapp-abc123", protocol="http")
        self.assertEqual(ctx.base_url, "http://myapp-abc123")

    def test_base_url_https(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", subdomain="myapp-abc123", protocol="https", ssl_enabled=True)
        self.assertEqual(ctx.base_url, "https://myapp-abc123")

    def test_local_url_none_when_no_port(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1")
        self.assertIsNone(ctx.local_url)

    def test_local_url_uses_port(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", port=4321)
        self.assertEqual(ctx.local_url, "http://127.0.0.1:4321")

    def test_missing_fields_complete(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", port=4321, subdomain="myapp-abc123")
        self.assertEqual(ctx.missing_fields(), [])

    def test_missing_fields_no_port(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", subdomain="myapp-abc123")
        self.assertIn("port", ctx.missing_fields())

    def test_missing_fields_no_subdomain(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", port=4321)
        self.assertIn("subdomain", ctx.missing_fields())

    def test_verify_for_deployment_raises_when_incomplete(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1")
        with self.assertRaises(PlatformContextError) as cm:
            ctx.verify_for_deployment()
        self.assertIn("port", cm.exception.missing)
        self.assertIn("subdomain", cm.exception.missing)

    def test_verify_for_deployment_passes_when_complete(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", port=5000, subdomain="myapp-abc123")
        ctx.verify_for_deployment()  # must not raise

    def test_as_dict_contains_all_keys(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", port=5000, subdomain="myapp-abc123", protocol="https")
        d = ctx.as_dict()
        for key in ("workspace_id", "port", "subdomain", "protocol", "gateway_available",
                    "ssl_enabled", "runtime_type", "base_url", "local_url"):
            self.assertIn(key, d)

    def test_protocol_http_when_ssl_disabled(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", ssl_enabled=False)
        self.assertEqual(ctx.protocol, "http")

    def test_protocol_https_when_ssl_enabled(self) -> None:
        ctx = WorkspaceContext(workspace_id="ws-1", ssl_enabled=True, protocol="https")
        self.assertEqual(ctx.protocol, "https")


class LoadWorkspaceContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_port_loaded_from_redis(self) -> None:
        stub = _RedisStub(port=4567)
        with (
            patch("services.redis_service.RedisService.get_sync_client", return_value=stub),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=1, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={"name": "myapp", "slug": "abc123"},
                server={},
            )
        self.assertEqual(ctx.port, 4567)

    async def test_gateway_available_when_in_active_set(self) -> None:
        stub = _RedisStub(port=4567, active=True)
        with (
            patch("services.redis_service.RedisService.get_sync_client", return_value=stub),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=1, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={"name": "myapp", "slug": "abc123"},
                server={},
            )
        self.assertTrue(ctx.gateway_available)

    async def test_subdomain_from_domain_field(self) -> None:
        stub = _RedisStub(port=4567)
        with (
            patch("services.redis_service.RedisService.get_sync_client", return_value=stub),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=1, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={"domain": "myapp-abc123", "name": "myapp", "slug": "abc123"},
                server={},
            )
        self.assertEqual(ctx.subdomain, "myapp-abc123")

    async def test_subdomain_derived_from_name_and_slug(self) -> None:
        stub = _RedisStub(port=4567)
        with (
            patch("services.redis_service.RedisService.get_sync_client", return_value=stub),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=1, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={"name": "My App!", "slug": "xyz789"},
                server={},
            )
        self.assertEqual(ctx.subdomain, "myapp-xyz789")

    async def test_protocol_http_when_no_cert(self) -> None:
        stub = _RedisStub(port=4567)
        with (
            patch("services.redis_service.RedisService.get_sync_client", return_value=stub),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=1, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={"name": "myapp", "slug": "abc123"},
                server={},
            )
        self.assertEqual(ctx.protocol, "http")
        self.assertFalse(ctx.ssl_enabled)

    async def test_protocol_https_when_cert_present(self) -> None:
        stub = _RedisStub(port=4567)
        with (
            patch("services.redis_service.RedisService.get_sync_client", return_value=stub),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=0, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={"name": "myapp", "slug": "abc123"},
                server={},
            )
        self.assertEqual(ctx.protocol, "https")
        self.assertTrue(ctx.ssl_enabled)

    async def test_redis_failure_degrades_gracefully(self) -> None:
        """Redis down must NOT crash load_workspace_context."""
        with (
            patch(
                "services.redis_service.RedisService.get_sync_client",
                side_effect=Exception("Redis unavailable"),
            ),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=1, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={"name": "myapp", "slug": "abc123"},
                server={},
            )
        self.assertIsNone(ctx.port)
        self.assertFalse(ctx.gateway_available)
        self.assertEqual(ctx.workspace_id, "ws-99")

    async def test_runtime_type_python_when_python_capable(self) -> None:
        stub = _RedisStub()
        with (
            patch("services.redis_service.RedisService.get_sync_client", return_value=stub),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=1, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={},
                server={},
                capabilities={"python": True, "node": False},
            )
        self.assertEqual(ctx.runtime_type, "python")

    async def test_runtime_type_node_when_only_node(self) -> None:
        stub = _RedisStub()
        with (
            patch("services.redis_service.RedisService.get_sync_client", return_value=stub),
            patch(
                "services.ssh_service.SSHService.execute",
                new_callable=AsyncMock,
                return_value=MagicMock(exit_code=1, stdout="", stderr=""),
            ),
        ):
            ctx = await load_workspace_context(
                workspace_id="ws-99",
                workspace={},
                server={},
                capabilities={"python": False, "node": True},
            )
        self.assertEqual(ctx.runtime_type, "node")


if __name__ == "__main__":
    unittest.main()
