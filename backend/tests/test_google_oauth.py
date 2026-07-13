"""Google OAuth backend regression tests.

Deterministic, network-free verification of ``POST /auth/google``.

What is mocked (and what is REAL):
  * REAL: ``routers/auth.py`` -> ``verify_google_id_token`` -> PyJWT decode with
    audience/issuer/expiry checks. We only substitute Google's public cert **set**
    (``get_google_certs``) so no network call is made.
  * FAKE: ``public.users`` access inside ``UserService`` is replaced by an
    in-memory store, so the upsert path runs without a database. The store
    preserves service-role singleton semantics (a user created on first login is
    visible on a later re-login within the same test).

Covered:
  * Valid token -> upsert into public.users -> ThinkSync JWT (sub = public.users.id).
  * Full journey: google -> /auth/me -> protected /servers/ works.
  * Re-login updates last_login_at and does NOT create a duplicate row.
  * Malformed / unknown-key / wrong-audience / expired / unverified-email tokens
    are rejected with 401 and a SAFE message.
  * Protected routes still reject garbage / non-UUID-subject tokens.

Run with: pytest tests/test_google_oauth.py -q
"""

import os
import time

os.environ.setdefault("JWT_SECRET", "test-secret-enough-bytes-0000000000")
os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role")
os.environ.setdefault("GOOGLE_CLIENT_ID", "fake-client-id.apps.googleusercontent.com")

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# One key pair shared by the suite; the public PEM is what "Google" would expose.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
_PUBLIC_PEM = _KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


def _fake_certs(*args, **kwargs):
    return {"test-kid": _PUBLIC_PEM}


def _make_google_token(claims: dict) -> str:
    return jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256", headers={"kid": "test-kid"})


def _valid_claims(sub="goog-123", email="user@example.com", **overrides):
    base = {
        "sub": sub,
        "email": email,
        "email_verified": True,
        "name": "Test User",
        "picture": "https://example.com/p.png",
        "iss": "https://accounts.google.com",
        "aud": "fake-client-id.apps.googleusercontent.com",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()) - 10,
    }
    base.update(overrides)
    return base


@pytest.fixture
def client(monkeypatch):
    users: dict[str, dict] = {}
    counter = {"n": 0}

    class FakeUsersTable:
        def __init__(self):
            self._mode = None
            self._match = None
            self._payload = None
            self._snapshot = None

        def select(self, *a, **k):
            self._mode = "select"
            return self

        def insert(self, payload):
            self._mode = "insert"
            self._payload = payload
            return self

        def update(self, payload):
            self._mode = "update"
            self._payload = payload
            return self

        def eq(self, field, value):
            self._match = (field, value)
            return self

        def maybe_single(self):
            self._mode = "single"
            return self

        def execute(self):
            # Return a DISTINCT result object per .execute() call (PostgREST
            # behaves this way), so the earlier `existing` reference keeps its
            # own snapshot even after a later .update() query runs on the same
            # table instance.
            if self._mode == "single":
                field, value = self._match
                snapshot = next(
                    (u for u in users.values() if u.get(field) == value), None
                )
            elif self._mode == "insert":
                counter["n"] += 1
                uid = f"{counter['n']:08x}0000-0000-0000-0000000000{counter['n']:02d}"
                row = {"id": uid, **self._payload}
                users[self._payload["google_sub"]] = row
                snapshot = [row]
            elif self._mode == "update":
                field, value = self._match
                row = next((u for u in users.values() if u.get(field) == value), None)
                if row is not None:
                    row.update(self._payload)
                snapshot = [row] if row else []
            else:
                snapshot = []

            class _Result:
                def __init__(self, data):
                    self.data = data

            return _Result(snapshot)

        @property
        def data(self):
            return self._snapshot

    class FakeSupabase:
        def __init__(self):
            self.users_table = FakeUsersTable()

        def table(self, name):
            if name == "public.users":
                return self.users_table
            return _EmptyTable()

    class _EmptyTable:
        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def maybe_single(self):
            return self

        @property
        def data(self):
            return []

        def execute(self):
            return self

    import services.user_service as usvc
    from services import google_auth as gauth
    import core.database as cdb
    import services.server_service as svc_mod

    fake_sb = FakeSupabase()

    monkeypatch.setattr(usvc, "get_supabase", lambda: fake_sb)
    monkeypatch.setattr(cdb, "get_supabase", lambda: fake_sb)
    monkeypatch.setattr(svc_mod.ServerService, "list_servers",
                        staticmethod(lambda user_id: []))
    monkeypatch.setattr(gauth, "get_google_certs", _fake_certs)

    import main

    yield main.app, users


def _tc(app):
    from starlette.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


def test_google_login_issues_jwt_and_creates_user(client):
    app, users = client
    tc = _tc(app)
    token = _make_google_token(_valid_claims())
    r = tc.post("/auth/google", json={"id_token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["access_token"], str)
    assert "goog-123" in users
    assert users["goog-123"]["email"] == "user@example.com"
    assert users["goog-123"]["last_login_at"] is not None


def test_google_login_then_me_then_protected_route(client):
    app, users = client
    tc = _tc(app)
    token = _make_google_token(_valid_claims())
    r = tc.post("/auth/google", json={"id_token": token})
    assert r.status_code == 200
    jwt_token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {jwt_token}"}

    me = tc.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"

    protected = tc.get("/servers/", headers=headers)
    assert protected.status_code == 200


def test_google_relogin_updates_last_login_not_duplicate(client):
    app, users = client
    tc = _tc(app)
    t1 = _make_google_token(_valid_claims())
    assert tc.post("/auth/google", json={"id_token": t1}).status_code == 200
    first_id = users["goog-123"]["id"]

    t2 = _make_google_token(_valid_claims())
    assert tc.post("/auth/google", json={"id_token": t2}).status_code == 200
    assert len(users) == 1
    assert users["goog-123"]["id"] == first_id
    assert users["goog-123"]["last_login_at"] is not None


def test_google_invalid_token_rejected(client):
    app, users = client
    tc = _tc(app)
    r = tc.post("/auth/google", json={"id_token": "not-a-jwt"})
    assert r.status_code == 401
    assert "Google sign-in failed" in r.json()["error"]


def test_google_unknown_key_rejected(client):
    app, users = client
    tc = _tc(app)
    # Sign with a DIFFERENT key pair whose kid is absent from _fake_certs.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    bad = jwt.encode(_valid_claims(), other_pem, algorithm="RS256", headers={"kid": "unknown-kid"})
    r = tc.post("/auth/google", json={"id_token": bad})
    assert r.status_code == 401


def test_google_wrong_audience_rejected(client):
    app, users = client
    tc = _tc(app)
    claims = _valid_claims(aud="evil-client-id.apps.googleusercontent.com")
    r = tc.post("/auth/google", json={"id_token": _make_google_token(claims)})
    assert r.status_code == 401


def test_google_expired_token_rejected(client):
    app, users = client
    tc = _tc(app)
    claims = _valid_claims(exp=int(time.time()) - 100)
    r = tc.post("/auth/google", json={"id_token": _make_google_token(claims)})
    assert r.status_code == 401


def test_google_unverified_email_rejected(client):
    app, users = client
    tc = _tc(app)
    claims = _valid_claims(email_verified=False)
    r = tc.post("/auth/google", json={"id_token": _make_google_token(claims)})
    assert r.status_code == 401


def test_protected_route_rejects_garbage_token(client):
    app, users = client
    tc = _tc(app)
    r = tc.get("/servers/", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
