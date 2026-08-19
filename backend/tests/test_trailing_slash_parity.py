"""Regression tests for removing the trailingSlash:true Next.js workaround.

Root cause this locks in
------------------------
The platform routers register their collection roots WITH a trailing slash
(``routers/servers.py`` -> ``"/servers/"``, ``routers/workspaces.py`` ->
``"/workspaces/"``, ``routers/jobs.py`` -> ``"/jobs/"``). A browser/fetch call
hitting the slash-less form (``/api/servers``) previously triggered a double
redirect: Next.js ``308`` (trailingSlash default false) + FastAPI
``redirect_slashes`` ``307`` whose ``Location`` used the rewrite Host
(``INTERNAL_API_URL`` = ``http://backend:8000`` / ``localhost:8000``,
unresolvable by the client) -> browser "Failed to fetch".

The historical band-aid was ``next.config.js: trailingSlash: true`` (suppressed
only the Next.js 308 and introduced auth regressions). The real fix is in the
backend: ``main.py`` now sets ``redirect_slashes=False`` and adds the
``normalize_collection_root_slash`` middleware, which rewrites the exact
collection roots (``/servers``, ``/workspaces``, ``/jobs``) to their canonical
slash form *in the ASGI scope* — so both "/servers" and "/servers/" resolve with
**no HTTP redirect**.

These tests assert:
  * ``redirect_slashes`` is False on the app.
  * The exact collection roots resolve with OR without a trailing slash.
  * No 3xx redirect is emitted for any of them (no ``Location`` header).
  * Non-collection slash-less paths are unaffected (no silent rewrite).
  * Workspace subdomains still enter the Gateway ( routing boundary untouched).
  * Platform hosts never enter the Gateway for these paths.

Run: pytest tests/test_trailing_slash_parity.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.testclient import TestClient  # noqa: E402

import main  # noqa: E402


GATEWAY_SENTINEL = "__gateway_entered__"


def _fake_gateway_proxy(path, request):
    from starlette.responses import PlainTextResponse

    async def _inner():
        return PlainTextResponse(GATEWAY_SENTINEL, status_code=299)

    return _inner()


# Paths the backend routers register WITH a trailing slash (collection roots).
COLLECTION_ROOTS = ["/servers", "/workspaces", "/jobs"]


class TestRedirectSlashesDisabled(unittest.TestCase):
    """The backend must NOT issue a 307 for slash-less collection roots."""

    def test_redirect_slashes_is_false(self) -> None:
        self.assertFalse(
            main.app.router.redirect_slashes,
            "redirect_slashes must be False so the backend never emits a 307 "
            "to the rewrite Host (root cause of the old 'Failed to fetch').",
        )

    def test_normalize_middleware_registered(self) -> None:
        names = [
            getattr(getattr(mw, "kwargs", {}).get("dispatch"), "__name__", "")
            for mw in main.app.user_middleware
        ]
        self.assertIn(
            "normalize_collection_root_slash",
            names,
            "normalize_collection_root_slash middleware must be registered.",
        )


class _ClientBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._patcher = patch.object(main, "_gateway_proxy", _fake_gateway_proxy)
        cls._patcher.start()
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._patcher.stop()


class TestCollectionRootSlashParity(_ClientBase):
    """Both '/servers' and '/servers/' must resolve with NO redirect."""

    def _assert_no_redirect(self, method: str, path: str, host: str = "app.thinksync.art") -> None:
        resp = self.client.request(
            method, path, headers={"host": host}, json={}, follow_redirects=False
        )
        self.assertNotIn(
            resp.status_code,
            (307, 308),
            f"{method} {path} must NOT redirect (got {resp.status_code}, "
            f"location={resp.headers.get('location')!r})",
        )
        self.assertNotIn(
            "location",
            {k.lower() for k in resp.headers.keys()},
            f"{method} {path} must not carry a Location header (no redirect).",
        )

    def test_get_roots_parity(self) -> None:
        for root in COLLECTION_ROOTS:
            self._assert_no_redirect("GET", root)
            self._assert_no_redirect("GET", root + "/")

    def test_post_roots_parity(self) -> None:
        for root in COLLECTION_ROOTS:
            self._assert_no_redirect("POST", root)
            self._assert_no_redirect("POST", root + "/")

    def test_auth_endpoints_unchanged_and_no_redirect(self) -> None:
        for path in ("/auth/login", "/auth/register", "/auth/logout"):
            self._assert_no_redirect("POST", path)
        # /auth/me is GET-only; just assert it reaches the app without redirect.
        resp = self.client.get("/auth/me", follow_redirects=False)
        self.assertNotEqual(resp.status_code, 307)

    def test_health_no_redirect(self) -> None:
        self._assert_no_redirect("GET", "/health")


class TestNonCollectionPathsNotRewritten(_ClientBase):
    """Only the exact collection roots are normalized; other paths are untouched."""

    def test_slashless_non_root_still_unmatched(self) -> None:
        # /chat is not a registered route and is NOT a collection root, so it
        # must NOT be silently rewritten to /chat/ — it should 404 (no redirect).
        resp = self.client.get("/chat", follow_redirects=False)
        self.assertNotEqual(resp.status_code, 307)
        self.assertEqual(resp.status_code, 404)

    def test_nested_slashless_paths_untouched(self) -> None:
        # /servers/123 (a detail path) is unaffected by the root normalizer.
        resp = self.client.get("/servers/123", follow_redirects=False)
        self.assertNotEqual(resp.status_code, 307)


class TestWorkspaceHostBoundaryUntouched(_ClientBase):
    """Workspace subdomains must still enter the Gateway for their whole URL space."""

    def _entered_gateway(self, method: str, path: str, host: str) -> bool:
        resp = self.client.request(
            method, path, headers={"host": host}, json={}, follow_redirects=False
        )
        return resp.status_code == 299 and GATEWAY_SENTINEL in resp.text

    def test_workspace_roots_enter_gateway(self) -> None:
        host = "project-abc123.thinksync.art"
        for path in ("/servers", "/servers/", "/workspaces", "/workspaces/", "/jobs", "/jobs/"):
            self.assertTrue(
                self._entered_gateway("GET", path, host),
                f"{path} on workspace host must enter Gateway (untouched).",
            )

    def test_platform_roots_never_enter_gateway(self) -> None:
        host = "app.thinksync.art"
        for path in ("/servers", "/servers/", "/workspaces", "/workspaces/", "/jobs", "/jobs/"):
            self.assertFalse(
                self._entered_gateway("POST", path, host),
                f"{path} on platform host must NOT enter Gateway.",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
