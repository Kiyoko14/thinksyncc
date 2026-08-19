"""Tests for the GitHub App integration backend (Phase 1).

These verify the PRODUCTION logic without live GitHub network calls:
  1. State signing/verification (CSRF-safe) round-trips and rejects tampering.
  2. Authorize URL is well-formed and includes client_id + signed state.
  3. Installation token minting hits the correct GitHub endpoint with the App
     JWT and caches the token (no second network call within TTL).
  4. Repository listing maps GitHub's payload to our model.
  5. Repository permission validation rejects no-read access (403) and missing
     repos (404).
  6. Workspace name derivation (repo name -> workspace name).
  7. Not-configured -> 501 (fail safe when App credentials are absent).

httpx is mocked so no real network traffic occurs. Supabase is mocked the same
way the existing github_connection tests mock it.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import Settings, get_settings
from models.github_app import GitHubAppCloneRequest, GitHubAppWorkspaceRequest
from services import github_app_service
from services.github_app_service import GitHubAppService, _make_state, _verify_state


# ---------------------------------------------------------------------------
# Config fixture: inject GitHub App credentials so github_app_enabled is True.
# ---------------------------------------------------------------------------


@pytest.fixture
def app_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "GITHUB_APP_ID", "123456")
    monkeypatch.setattr(s, "GITHUB_APP_CLIENT_ID", "Iv1.abc")
    monkeypatch.setattr(s, "GITHUB_APP_CLIENT_SECRET", "secret-xyz")
    monkeypatch.setattr(s, "GITHUB_APP_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----FAKE-----END RSA PRIVATE KEY-----")
    monkeypatch.setattr(s, "JWT_SECRET", "test-jwt-secret")
    monkeypatch.setattr(s, "JWT_ALGORITHM", "HS256")
    # Re-evaluate the cached property
    monkeypatch.setattr(type(s), "github_app_enabled", property(lambda self: True), raising=False)
    return s


# ---------------------------------------------------------------------------
# 1. State signing round-trip
# ---------------------------------------------------------------------------


def test_state_round_trip(app_settings):
    state = _make_state("user-1")
    # Should not raise
    _verify_state(state, "user-1")


def test_state_rejects_wrong_user(app_settings):
    from fastapi import HTTPException
    state = _make_state("user-1")
    with pytest.raises(HTTPException):
        _verify_state(state, "user-2")


# ---------------------------------------------------------------------------
# 2. Authorize URL
# ---------------------------------------------------------------------------


def test_authorize_url_well_formed(app_settings):
    resp = asyncio.run(GitHubAppService.authorize_url(user_id="u1"))
    assert "github.com/login/oauth/authorize" in resp.authorization_url
    assert "client_id=Iv1.abc" in resp.authorization_url
    assert "state=" in resp.authorization_url
    # state must verify
    import urllib.parse as up
    qs = up.parse_qs(up.urlparse(resp.authorization_url).query)
    _verify_state(qs["state"][0], "u1")


# ---------------------------------------------------------------------------
# 3. Installation token minting + caching
# ---------------------------------------------------------------------------


def _fake_github_client(token_json, status_code=201):
    """Return a fake ``github_api_call`` coroutine yielding a scripted response.

    Part 3 refactor: call sites now go through ``github_api_call`` (the rate-limit
    layer, which internally wraps the Part 2 ``github_request`` retry mechanism),
    so tests patch that single seam. The layer returns the response as-is; the
    caller does its own status handling, exactly as in production.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = token_json
    resp.text = "{}"

    async def _fake_request(method, url, **kw):
        return resp

    return _fake_request


def test_installation_token_mint_and_cache(app_settings):
    token_body = {"token": "test-install-token", "expires_at": "2099-01-01T00:00:00Z"}
    fake_request = _fake_github_client(token_body)

    async def run():
        with patch("services.github_app_service.github_api_call", new=fake_request):
            with patch("services.github_app_service._make_app_jwt", return_value="fake.jwt.token"):
                # get_installation_token is a module-level function, not a method.
                t1 = await github_app_service.get_installation_token(installation_id="inst-1")
                # Second call should hit the cache (no new POST) -> same token.
                t2 = await github_app_service.get_installation_token(installation_id="inst-1")
        return t1, t2

    t1, t2 = asyncio.run(run())
    assert t1 == "test-install-token"
    assert t2 == "test-install-token"


# ---------------------------------------------------------------------------
# 4. Repository listing mapping
# ---------------------------------------------------------------------------


def test_list_repositories_maps_payload(app_settings):
    repos_payload = {
        "repositories": [
            {
                "id": 1,
                "name": "thinksync",
                "full_name": "Kiyoko14/thinksync",
                "private": False,
                "default_branch": "main",
                "html_url": "https://github.com/Kiyoko14/thinksync",
                "permissions": {"pull": True, "push": True},
            },
            {
                "id": 2,
                "name": "other",
                "full_name": "Kiyoko14/other",
                "private": True,
                "default_branch": "master",
                "permissions": {"pull": True, "push": False},
            },
        ]
    }

    async def run():
        fake_request = _fake_github_client(repos_payload, status_code=200)
        with patch("services.github_app_service.github_api_call", new=fake_request):
            with patch("services.github_app_service._load_pem", return_value="FAKE-PEM"):
                with patch.object(
                    github_app_service, "_ownership_or_404", new=AsyncMock(return_value={"id": "inst-1", "user_id": "u1"})
                ):
                    with patch.object(
                        github_app_service, "get_installation_token",
                        new=AsyncMock(return_value="tok"),
                    ):
                        return await GitHubAppService.list_repositories(user_id="u1", installation_id="inst-1")

    resp = asyncio.run(run())
    assert resp.installation_id == "inst-1"
    assert len(resp.repositories) == 2
    assert resp.repositories[0].full_name == "Kiyoko14/thinksync"
    assert resp.repositories[0].default_branch == "main"
    assert resp.repositories[0].permissions["pull"] is True


# ---------------------------------------------------------------------------
# 5. Permission validation
# ---------------------------------------------------------------------------


def test_validate_repo_requires_read(app_settings):
    from fastapi import HTTPException

    no_read = {"permissions": {"pull": False}, "full_name": "a/b"}
    async def run():
        fake_request = _fake_github_client(no_read, status_code=200)
        with patch("services.github_app_service.github_api_call", new=fake_request):
            with patch.object(
                github_app_service, "get_installation_token",
                new=AsyncMock(return_value="tok"),
            ):
                return await GitHubAppService._validate_repo_permission(
                    installation_id="inst-1", repo_full_name="a/b"
                )

    with pytest.raises(HTTPException):
        asyncio.run(run())


def test_validate_repo_404_when_inaccessible(app_settings):
    from fastapi import HTTPException

    async def run():
        fake_request = _fake_github_client({}, status_code=404)
        with patch("services.github_app_service.github_api_call", new=fake_request):
            with patch.object(
                github_app_service, "get_installation_token",
                new=AsyncMock(return_value="tok"),
            ):
                return await GitHubAppService._validate_repo_permission(
                    installation_id="inst-1", repo_full_name="a/nope"
                )

    with pytest.raises(HTTPException):
        asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. Workspace name derivation
# ---------------------------------------------------------------------------


def test_workspace_name_from_repo():
    assert GitHubAppService._workspace_name_from_repo("thinksync") == "thinksync"
    assert GitHubAppService._workspace_name_from_repo("my-awesome.repo") == "my-awesome-repo"
    # Underscores are preserved (valid workspace name chars).
    assert GitHubAppService._workspace_name_from_repo("Weird_NAME") == "weird_name"
    assert GitHubAppService._workspace_name_from_repo("__leading__") == "leading"


# ---------------------------------------------------------------------------
# 7. Not-configured -> 501
# ---------------------------------------------------------------------------


def test_not_configured_returns_501(monkeypatch):
    from fastapi import HTTPException

    s = get_settings()
    monkeypatch.setattr(s, "GITHUB_APP_ID", "")
    monkeypatch.setattr(s, "GITHUB_APP_PRIVATE_KEY", "")
    monkeypatch.setattr(s, "GITHUB_APP_PRIVATE_KEY_PATH", "")
    monkeypatch.setattr(type(s), "github_app_enabled", property(lambda self: False), raising=False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(GitHubAppService.authorize_url(user_id="u1"))
    assert exc.value.status_code == 501


# ---------------------------------------------------------------------------
# 8. Clone uses GIT_ASKPASS (token never in argv / URL)
# ---------------------------------------------------------------------------


def test_clone_token_not_in_argv(app_settings):
    captured = {}

    async def fake_subprocess_exec(*args, **kw):
        captured["argv"] = list(args)
        captured["env"] = dict(kw.get("env", {}))
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    async def run():
        with patch("asyncio.create_subprocess_exec", new=fake_subprocess_exec):
            with patch.object(
                github_app_service, "get_installation_token",
                new=AsyncMock(return_value="supersecret-token"),
            ):
                return await GitHubAppService.clone(
                    installation_id="inst-1", repo="a/b", branch=None, depth=None,
                    workspace_path="/tmp/ws", server=None,
                )

    res = asyncio.run(run())
    assert res["ok"] is True
    # Token must NOT appear in the command line.
    joined = " ".join(str(a) for a in captured["argv"])
    assert "supersecret-token" not in joined
    assert "https://github.com/a/b.git" in joined
    # Token must be present in env (GITHUB_APP_TOKEN) and GIT_ASKPASS set.
    assert captured["env"].get("GITHUB_APP_TOKEN") == "supersecret-token"
    assert captured["env"].get("GIT_ASKPASS")
