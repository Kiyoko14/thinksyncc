"""Regression tests for the host-scoped workspace Gateway routing boundary.

Root cause under test
---------------------
Commit ``d016a14`` ("config file") changed the Gateway catch-all route from
``/gateway/{path:path}`` to the global ``/{path:path}``. The global catch-all
then *shadowed* every platform API route (``/servers``, ``/workspaces``,
``/auth/*`` …): a request to ``POST /servers`` (no trailing slash) matched the
Gateway catch-all before FastAPI's ``redirect_slashes`` could redirect it to
``/servers/``, so it fell into the Gateway proxy and returned
``{"error": "No subdomain"}`` instead of reaching the servers router.

The fix restores the architectural isolation WITHOUT bringing back the legacy
``/gateway/*`` URL prefix, and WITHOUT relying on the in-Gateway reserved-domain
check as the routing mechanism. The boundary is now enforced by HOST in
``main.route_workspace_hosts_to_gateway`` (an HTTP middleware that runs before
routing):

  * platform / reserved / apex hosts  -> normal FastAPI routing (Gateway never runs)
  * genuine workspace subdomains       -> dispatched straight to the Gateway proxy
                                          for their ENTIRE URL space

The Gateway proxy handler and its ``_is_workspace_host`` predicate live in
``routers/gateway.py`` and were not otherwise changed; ``gateway.router`` is no
longer registered as a global catch-all route.

These tests exercise the predicate directly and the boundary via Starlette's
``TestClient`` with a mocked workspace lookup (no Redis / Supabase / network).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure the backend package root is importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from routers.gateway import _is_workspace_host  # noqa: E402


# Canonical hosts used across the suite.
WORKSPACE_HOSTS = [
    "project-abc123.thinksync.art",
    "myapp-xyz789.thinksync.art",
    "project-abc123.thinksync.art:80",
    "PROJECT-ABC123.thinksync.art",  # case-insensitive
]
PLATFORM_HOSTS = [
    "app.thinksync.art",
    "api.thinksync.art",
    "www.thinksync.art",
    "thinksync.art",
    "app.thinksync.art:443",
    "localhost",
    "127.0.0.1",
    "evil.com",
    "notaworkspace.thinksync.art",  # wrong subdomain shape
]
PLATFORM_PATHS = ["/servers", "/workspaces", "/auth/login", "/commands/execute", "/jobs"]

# Sentinel the patched Gateway proxy returns so tests can assert "entered Gateway"
# without standing up Redis / Supabase / the real workspace runtime.
GATEWAY_SENTINEL = "__gateway_entered__"


def _fake_gateway_proxy(path, request):
    from starlette.responses import PlainTextResponse

    async def _inner():
        return PlainTextResponse(GATEWAY_SENTINEL, status_code=299)

    return _inner()


class TestWorkspaceHostPredicate(unittest.TestCase):
    """The host predicate that drives the whole routing boundary."""

    def test_workspace_hosts_are_workspace(self) -> None:
        for h in WORKSPACE_HOSTS:
            self.assertTrue(_is_workspace_host(h), f"{h!r} should be a workspace host")

    def test_platform_hosts_are_not_workspace(self) -> None:
        for h in PLATFORM_HOSTS + [""]:
            self.assertFalse(_is_workspace_host(h), f"{h!r} must NOT be a workspace host")

    def test_reserved_names_never_workspace(self) -> None:
        for name in ("app", "api", "www"):
            self.assertFalse(_is_workspace_host(f"{name}.thinksync.art"))

    def test_missing_host_is_platform(self) -> None:
        self.assertFalse(_is_workspace_host(""))


class _BoundaryTestBase(unittest.TestCase):
    """Shared TestClient with the Gateway proxy patched to a sentinel."""

    @classmethod
    def setUpClass(cls) -> None:
        # Patch the proxy reference captured by the middleware in main.
        cls._patcher = patch.object(main, "_gateway_proxy", _fake_gateway_proxy)
        cls._patcher.start()
        # raise_server_exceptions=False so a platform 401/400/404 doesn't blow up.
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._patcher.stop()

    def _entered_gateway(self, method: str, path: str, host: str) -> bool:
        resp = self.client.request(
            method, path, headers={"host": host}, json={}, follow_redirects=False
        )
        return resp.status_code == 299 and GATEWAY_SENTINEL in resp.text


class TestPlatformRequestsNeverEnterGateway(_BoundaryTestBase):
    """Regression #1: platform API requests must never enter the Gateway."""

    def test_platform_paths_no_trailing_slash(self) -> None:
        # The exact case that regressed: no trailing slash used to hit the
        # catch-all before redirect_slashes could act.
        for host in PLATFORM_HOSTS:
            for path in PLATFORM_PATHS:
                self.assertFalse(
                    self._entered_gateway("POST", path, host),
                    f"POST {path} (Host={host!r}) must NOT enter Gateway",
                )

    def test_platform_paths_with_trailing_slash(self) -> None:
        for host in PLATFORM_HOSTS:
            for path in PLATFORM_PATHS:
                self.assertFalse(
                    self._entered_gateway("POST", path + "/", host),
                    f"POST {path}/ (Host={host!r}) must NOT enter Gateway",
                )

    def test_platform_health_still_served(self) -> None:
        # A platform host hitting a real platform route works normally.
        resp = self.client.get("/health", headers={"host": "app.thinksync.art"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(GATEWAY_SENTINEL, resp.text)


class TestWorkspaceRequestsAlwaysEnterGateway(_BoundaryTestBase):
    """Regression #2: real workspace subdomains always enter the Gateway."""

    def test_workspace_root(self) -> None:
        for host in WORKSPACE_HOSTS:
            self.assertTrue(
                self._entered_gateway("GET", "/", host),
                f"GET / (Host={host!r}) must enter Gateway",
            )

    def test_workspace_owns_entire_url_space(self) -> None:
        # Critically, workspace routes that COLLIDE with platform paths
        # (/health, /servers) must still be proxied to the workspace, not
        # shadowed by a platform router.
        for host in WORKSPACE_HOSTS:
            for path in ("/", "/api/data", "/static/app.js", "/servers", "/health", "/auth/login"):
                self.assertTrue(
                    self._entered_gateway("GET", path, host),
                    f"GET {path} (Host={host!r}) must enter Gateway (workspace owns its whole URL space)",
                )


class TestTrailingSlashParity(_BoundaryTestBase):
    """Regression #3: trailing / non-trailing slash behave identically per host class."""

    def test_platform_parity(self) -> None:
        for path in PLATFORM_PATHS:
            self.assertFalse(self._entered_gateway("POST", path, "app.thinksync.art"))
            self.assertFalse(self._entered_gateway("POST", path + "/", "app.thinksync.art"))

    def test_workspace_parity(self) -> None:
        host = "project-abc123.thinksync.art"
        for path in ("/servers", "/servers/", "/api/data", "/api/data/", "/health", "/health/"):
            self.assertTrue(
                self._entered_gateway("GET", path, host),
                f"{path} on workspace host must enter Gateway",
            )

    def test_platform_no_slash_redirects_not_gateway(self) -> None:
        # POST /servers (no slash) on a platform host must resolve directly to
        # the servers router (401 = reached auth) with NO redirect and must NOT
        # enter the Gateway. The old redirect_slashes 307 to the rewrite Host was
        # the root cause of the client "Failed to fetch"; it is now eliminated by
        # main.normalize_collection_root_slash (redirect_slashes=False).
        resp = self.client.post(
            "/servers", headers={"host": "app.thinksync.art"}, json={}, follow_redirects=False
        )
        self.assertNotEqual(resp.status_code, 307)
        self.assertNotIn("location", {k.lower() for k in resp.headers.keys()})
        self.assertNotIn(GATEWAY_SENTINEL, resp.text)


class TestDeploymentRoutingUnchanged(_BoundaryTestBase):
    """Regression #4 & #5: Gateway proxy contract + platform routing intact.

    The Gateway proxy handler itself is untouched; only *who* reaches it
    changed. We assert the middleware exists, dispatches whole workspace
    requests to the proxy, and that the legacy ``/gateway/*`` prefix was NOT
    reintroduced and the global catch-all route is NOT registered (its
    presence was the regression).
    """

    def test_boundary_middleware_registered(self) -> None:
        names = []
        for mw in main.app.user_middleware:
            fn = getattr(mw, "kwargs", {}).get("dispatch")
            if fn is not None:
                names.append(getattr(fn, "__name__", ""))
        self.assertIn("route_workspace_hosts_to_gateway", names)

    def test_no_global_catch_all_route(self) -> None:
        # The proxy must NOT be registered as an app route (that catch-all is
        # exactly what shadowed platform routes in d016a14).
        paths = []
        for included in main.app.router.routes:
            orig = getattr(included, "original_router", None)
            if orig is not None:
                paths.extend(getattr(r, "path", "") for r in getattr(orig, "routes", []))
            else:
                paths.append(getattr(included, "path", ""))
        self.assertNotIn("/{path:path}", paths, "global catch-all must not be registered")
        self.assertFalse(
            any(str(p).startswith("/gateway/") for p in paths),
            "legacy /gateway/* prefix must not be reintroduced",
        )

    def test_gateway_proxy_handler_untouched(self) -> None:
        # The real proxy handler still exists and is what the middleware calls.
        from routers.gateway import proxy_request
        self.assertTrue(callable(proxy_request))


if __name__ == "__main__":
    unittest.main(verbosity=2)
