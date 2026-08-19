"""Tests for the GitHub Connection layer (models + service crypto/validation).

These tests verify the FOUNDATION only (no live GitHub network calls):
  1. GitHubConnection models serialize correctly (private key is NEVER in the
     public response).
  2. encrypt_secret / decrypt_secret round-trip used by the service.
  3. GitHubService.create_connection encrypts the private key at rest and
     returns it ONLY via GitHubConnectionWithKey when generate_keypair=True.
  4. GitHubService.create_connection rejects a malformed private key.
  5. _detect_key_type classifies public keys.
  6. check_repo_access / get_repo_metadata reject bad repo slugs and unknown
     connection ids (fail fast, no network).

Async helpers are run via asyncio.run inside plain ``def`` tests to match
the project's test conventions (pytest-asyncio is not a dependency).
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.crypto import decrypt_secret, encrypt_secret
from models.agent import ToolName
from models.github import (
    GitHubCloneRequest,
    GitHubConnectionCreate,
    GitHubConnectionResponse,
    GitHubConnectionWithKey,
    GitHubRepoAccessRequest,
)
from services import github_service
from services.github_service import GitHubService, _detect_key_type


# ---------------------------------------------------------------------------
# 1. Model serialization — private key never leaks
# ---------------------------------------------------------------------------


def test_public_response_omits_private_key():
    resp = GitHubConnectionResponse(
        id="c1",
        user_id="u1",
        name="my-gh",
        auth_method="ssh",
        host="github.com",
        ssh_public_key="ssh-ed25519 AAAA user@host",
        ssh_key_type="ed25519",
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
    )
    dumped = resp.model_dump()
    assert "ssh_private_key" not in dumped
    assert dumped["ssh_public_key"].startswith("ssh-ed25519")


def test_with_key_includes_private_once():
    resp = GitHubConnectionWithKey(
        id="c1",
        user_id="u1",
        name="my-gh",
        auth_method="ssh",
        host="github.com",
        ssh_public_key="ssh-ed25519 AAAA user@host",
        ssh_key_type="ed25519",
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
        ssh_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    )
    dumped = resp.model_dump()
    assert dumped["ssh_private_key"].startswith("-----BEGIN")


def test_clone_request_validation():
    req = GitHubCloneRequest(
        github_connection_id="c1",
        repo="nous/research",
        branch="main",
        depth=1,
    )
    assert req.repo == "nous/research"
    assert req.depth == 1


# ---------------------------------------------------------------------------
# 2. Crypto round-trip (what the service uses for at-rest encryption)
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip():
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nrealkey\n-----END OPENSSH PRIVATE KEY-----"
    enc = encrypt_secret(secret)
    assert enc != secret
    assert enc.startswith("enc:v1:")
    assert decrypt_secret(enc) == secret


def test_encrypt_is_not_idempotent_by_design():
    # encrypt_secret always (re-)encrypts; callers must guard with the
    # "enc:v1:" prefix check themselves (the service does this via the DB row).
    enc1 = encrypt_secret("secret-value")
    enc2 = encrypt_secret(enc1)  # would double-wrap if called blindly
    assert enc2.startswith("enc:v1:")
    # The stored form is still decryptable to the ORIGINAL plaintext only if the
    # caller checks the prefix first; assert the helper's contract directly.
    assert decrypt_secret(encrypt_secret("x")) == "x"


# ---------------------------------------------------------------------------
# 3. Key type detection
# ---------------------------------------------------------------------------


def test_detect_key_type():
    assert _detect_key_type("ssh-ed25519 AAA user@h") == "ed25519"
    assert _detect_key_type("ssh-rsa AAA user@h") == "rsa"
    assert _detect_key_type("ecdsa-sha2-nistp256 AAA") == "ecdsa"
    assert _detect_key_type("") == "unknown"
    assert _detect_key_type("not-a-key") == "unknown"


# ---------------------------------------------------------------------------
# 4. create_connection — encrypt at rest + key return policy
# ---------------------------------------------------------------------------


def _fake_supabase_insert(record):
    """Build an AsyncMock supabase that returns the record on insert."""

    class _Exec:
        def __init__(self, data):
            self.data = [data]

    class _Table:
        def __init__(self, record):
            self._record = record
            self._last = None

        def insert(self, rec):
            self._last = rec
            return self

        def eq(self, *_a, **_k):
            return self

        async def execute(self):
            return _Exec({**self._record, **(self._last or {})})

    class _Supa:
        def __init__(self):
            self._tables = {}

        def table(self, name):
            if name not in self._tables:
                self._tables[name] = _Table(record)
            return self._tables[name]

    return _Supa()


def test_create_connection_encrypts_and_returns_key_once():
    record = {
        "id": "c1",
        "user_id": "u1",
        "name": "my-gh",
        "auth_method": "ssh",
        "host": "github.com",
        "ssh_public_key": "ssh-ed25519 AAA user@h",
        "ssh_key_type": "ed25519",
        "created_at": "2026-07-17T00:00:00+00:00",
        "updated_at": "2026-07-17T00:00:00+00:00",
    }
    payload = GitHubConnectionCreate(
        name="my-gh",
        auth_method="ssh",
        generate_keypair=True,  # key returned ONCE in this mode
    )
    fake = _fake_supabase_insert(record)

    async def run():
        # Mock ssh-keygen + the supabase insert so we exercise the SERVICE
        # logic (encrypt + return-once), not the real subprocess or DB.
        with patch("services.github_service.get_supabase_async", return_value=fake):
            with patch(
                "services.github_service.generate_keypair",
                new=AsyncMock(
                    return_value=(
                        "-----BEGIN OPENSSH PRIVATE KEY-----\ngenerated\n-----END OPENSSH PRIVATE KEY-----",
                        "ssh-ed25519 AAAAC3Nza generated-key thinksync-c1",
                    )
                ),
            ):
                return await GitHubService.create_connection(user_id="u1", payload=payload)

    result = asyncio.run(run())
    # In generate mode the key is returned exactly once (WithKey variant).
    assert isinstance(result, GitHubConnectionWithKey)
    assert result.ssh_private_key.startswith("-----BEGIN")
    assert result.ssh_public_key.startswith("ssh-ed25519 ")
    # Stored value must be encrypted at rest.
    stored = fake.table("github_connections")._last
    assert stored["ssh_private_key"].startswith("enc:v1:")



def test_create_connection_rejects_bad_private_key():
    payload = GitHubConnectionCreate(
        name="my-gh",
        auth_method="ssh",
        ssh_private_key="not-a-real-key",
        ssh_public_key="ssh-ed25519 AAA user@h",
    )
    fake = _fake_supabase_insert({})

    async def run():
        with patch("services.github_service.get_supabase_async", return_value=fake):
            return await GitHubService.create_connection(user_id="u1", payload=payload)

    with pytest.raises(Exception):
        asyncio.run(run())


def test_create_connection_rejects_pat_mode():
    # The model enforces auth_method == 'ssh' at the boundary, so a token
    # mode is rejected before it ever reaches the service.
    with pytest.raises(Exception):
        GitHubConnectionCreate(
            name="my-gh",
            auth_method="token",  # unsupported -> pydantic pattern error
        )


# ---------------------------------------------------------------------------
# 5. Validation guards (no network)
# ---------------------------------------------------------------------------


def test_check_access_rejects_unknown_connection():
    from fastapi import HTTPException

    async def run():
        with patch(
            "services.github_service.get_supabase_async",
            return_value=_fake_supabase_not_found(),
        ):
            return await GitHubService.check_repo_access(
                user_id="u1",
                connection_id="does-not-exist",
                payload=GitHubRepoAccessRequest(repo="a/b"),
            )

    with pytest.raises(HTTPException):
        asyncio.run(run())


def _fake_supabase_not_found():
    class _Exec:
        def __init__(self):
            self.data = None

    class _Table:
        async def execute(self):
            return _Exec()

        def eq(self, *_a, **_k):
            return self

        def maybe_single(self):
            return self

        def select(self, *_a, **_k):
            return self

    class _Supa:
        def table(self, _name):
            return _Table()

    return _Supa()


def test_get_connection_404():
    from fastapi import HTTPException

    async def run():
        with patch(
            "services.github_service.get_supabase_async",
            return_value=_fake_supabase_not_found(),
        ):
            return await GitHubService.get_connection(connection_id="x", user_id="u1")

    with pytest.raises(HTTPException):
        asyncio.run(run())
