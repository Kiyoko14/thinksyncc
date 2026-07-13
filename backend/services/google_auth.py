"""Google ID-token verification for the ThinkSync backend.

ThinkSync uses Google OAuth as the *only* authentication method. The browser
obtains a Google ID token (a signed JWT) and posts it to ``POST /auth/google``.
This module verifies the token cryptographically against Google's public
signing certificates and returns the decoded claims (sub, email, name, picture).

We deliberately do NOT use the Supabase/Google client libraries for the
token exchange: the backend is the source of truth for identities and verifies
the token directly with PyJWT (already a dependency) so no client secret is
required and Supabase Auth is fully bypassed.

The Google certs endpoint is fetched once and cached in-process; callers pass
a ``fetch_certs`` callable in tests so the verification path can run without
network access.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import jwt

from core.config import get_settings

logger = logging.getLogger(__name__)

# Certs are rotated regularly; cache them for a short window.
_CERTS_CACHE: dict[str, Any] | None = None
_CERTS_FETCHED_AT: float = 0.0
_CERTS_TTL_SECONDS = 3600


class GoogleTokenError(Exception):
    """Raised when a Google ID token cannot be verified."""


def _default_fetch_certs() -> dict[str, Any]:
    """Fetch Google's public signing certificates (PEM keyed by kid)."""
    import urllib.request

    url = get_settings().GOOGLE_CERTS_URL
    req = urllib.request.Request(url, headers={"User-Agent": "thinksync-backend"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - HTTPS, fixed host
        raw = resp.read().decode("utf-8")
    import json

    return json.loads(raw)


def get_google_certs(fetch_certs: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return Google's signing certs, caching them in-process for one hour.

    ``fetch_certs`` is an injection point used by tests to avoid network access.
    """
    global _CERTS_CACHE, _CERTS_FETCHED_AT
    now = time.time()
    if _CERTS_CACHE is not None and (now - _CERTS_FETCHED_AT) < _CERTS_TTL_SECONDS:
        return _CERTS_CACHE
    fetcher = fetch_certs or _default_fetch_certs
    _CERTS_CACHE = fetcher()
    _CERTS_FETCHED_AT = now
    return _CERTS_CACHE


def verify_google_id_token(
    id_token: str,
    *,
    fetch_certs: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify a Google-signed OIDC ID token and return its claims.

    Args:
        id_token: the raw JWT string from the Google sign-in response.
        fetch_certs: optional callable returning Google's certs dict (used to
            inject a mock in tests so no network is required). In production this
            is normally left as ``None`` and Google's certs are fetched + cached
            via ``get_google_certs``.

    Returns:
        The decoded claims dict (contains ``sub``, ``email``, ``email_verified``,
        ``name``, ``picture``, ``iss``, ``aud``, ``exp``).

    Raises:
        GoogleTokenError: if the token is missing, malformed, has the wrong
            audience/issuer, is expired, or is signed by an unknown key.
    """
    settings = get_settings()
    client_id = settings.GOOGLE_CLIENT_ID
    if not client_id:
        # Misconfiguration: we cannot verify the audience without a client id.
        raise GoogleTokenError("GOOGLE_CLIENT_ID is not configured on the server.")

    if not id_token or not isinstance(id_token, str):
        raise GoogleTokenError("Missing Google ID token.")

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.InvalidTokenError as exc:
        raise GoogleTokenError(f"Malformed Google ID token: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise GoogleTokenError("Google ID token is missing the 'kid' header.")

    certs = get_google_certs(fetch_certs=fetch_certs)
    public_key = certs.get(kid)
    if public_key is None:
        # Cert may have rotated; force a refresh and retry once.
        global _CERTS_CACHE
        _CERTS_CACHE = None
        certs = get_google_certs(fetch_certs=fetch_certs)
        public_key = certs.get(kid)
        if public_key is None:
            raise GoogleTokenError("Google ID token signed by an unknown key (kid not found).")

    try:
        claims = jwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=settings.GOOGLE_ISSUERS,
            options={"verify_exp": True, "verify_aud": True, "verify_iss": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise GoogleTokenError("Google ID token has expired.") from exc
    except jwt.InvalidAudienceError as exc:
        raise GoogleTokenError("Google ID token audience does not match GOOGLE_CLIENT_ID.") from exc
    except jwt.InvalidIssuerError as exc:
        raise GoogleTokenError("Google ID token issuer is not a trusted Google issuer.") from exc
    except jwt.InvalidTokenError as exc:
        raise GoogleTokenError(f"Google ID token verification failed: {exc}") from exc

    # Require a verified email (Google only sets email_verified for Google
    # accounts; hosted-domain / unverified emails are rejected).
    if not claims.get("email"):
        raise GoogleTokenError("Google ID token has no email claim.")
    if claims.get("email_verified") is False:
        raise GoogleTokenError("Google email is not verified.")

    return claims
