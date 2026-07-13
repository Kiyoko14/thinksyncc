from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from supabase_auth.errors import AuthApiError

from core.database import get_supabase
from core.security import create_access_token, get_current_user
from models.user import (
    GoogleLoginRequest,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserResponse,
)
from services.google_auth import GoogleTokenError, verify_google_id_token
from services.user_service import UserService

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_http_error(
    *,
    status_code: int,
    code: str,
    message: str,
    meta: dict[str, Any] | None = None,
) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if meta:
        detail["meta"] = meta
    return HTTPException(status_code=status_code, detail=detail)


@router.post("/google", response_model=TokenResponse)
async def google_login(
    payload: GoogleLoginRequest,
    fetch_certs=None,
) -> TokenResponse:
    """Exchange a verified Google ID token for a ThinkSync JWT.

    The backend verifies the token cryptographically against Google's public
    certs, upserts the user into ``public.users`` (creating on first login,
    refreshing ``last_login_at`` on return), and issues a ThinkSync HS256 JWT
    whose ``sub`` is the ``public.users.id``. No password is ever involved.

    ``fetch_certs`` is an injection point used by tests to avoid network access;
    production never passes it.
    """
    try:
        claims = verify_google_id_token(payload.id_token, fetch_certs=fetch_certs)
    except GoogleTokenError as exc:
        raise _auth_http_error(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="OAUTH_TOKEN_INVALID",
            message="Google sign-in failed. Please try again.",
            meta={"error_type": "GoogleTokenError"},
        )

    user = UserService.upsert_from_google(claims)

    token = create_access_token(
        subject=str(user["id"]),
        extra_data={"email": user.get("email")},
    )
    return TokenResponse(access_token=token)


# ─────────────────────────────────────────────────────────────
# REGISTER
# ─────────────────────────────────────────────────────────────
@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest) -> RegisterResponse:
    supabase = get_supabase()

    try:
        response = supabase.auth.sign_up({
            "email": payload.email,
            "password": payload.password,
        })

    except Exception as e:
        import logging
        import traceback
        import httpx

        logging.getLogger(__name__).error(
            "[auth] register_failed | email=%s | err_type=%s | err=%r",
            payload.email,
            type(e).__name__,
            str(e),
            exc_info=True,
        )
        traceback.print_exc()

        # Network failures to Supabase must be distinct (502) so callers can
        # tell "service down" apart from a client-side problem.
        if isinstance(e, httpx.HTTPError):
            raise _auth_http_error(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code="NETWORK_ERROR",
                message="Registration service is temporarily unavailable. Please try again.",
                meta={"error_type": type(e).__name__},
            )

        # A duplicate account is a client error (400) with a clear message,
        # never surfaced as a 401 "invalid credentials".
        if isinstance(e, AuthApiError):
            code_attr = getattr(e, "code", None)
            if code_attr == "user_already_exists":
                raise _auth_http_error(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code="EMAIL_EXISTS",
                    message="An account with this email already exists. Please sign in instead.",
                    meta={"error_type": type(e).__name__},
                )

        raise _auth_http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="AUTH_FAILED",
            message="Registration failed. Please try again or contact support.",
            meta={"error_type": type(e).__name__},
        )

    # Supabase must return a user; otherwise registration truly failed.
    if not response or not response.user:
        raise _auth_http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="AUTH_FAILED",
            message="Registration failed: no user was created. Please try again or contact support.",
        )

    # Email confirmation is enabled in Supabase: sign_up returns the user but no
    # session, so we cannot mint a backend JWT yet. Hand back a clear signal and
    # NO token (a token without a confirmed Supabase session is a phantom session
    # that later fails on login with "Invalid login credentials").
    if not getattr(response, "session", None):
        return RegisterResponse(
            access_token=None,
            token_type="bearer",
            requires_confirmation=True,
        )

    # Confirmed path: Supabase returned a session, so mint the backend JWT.
    token = create_access_token(
        subject=str(response.user.id),
        extra_data={"email": response.user.email},
    )

    return RegisterResponse(access_token=token)


# ─────────────────────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────────────────────
@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    supabase = get_supabase()

    try:
        response = supabase.auth.sign_in_with_password({
            "email": payload.email,
            "password": payload.password,
        })

    except Exception as e:
        import logging
        import traceback
        import httpx

        logging.getLogger(__name__).error(
            "[auth] login_failed | email=%s | password_len=%s | err_type=%s | err=%r",
            payload.email,
            len(payload.password or ""),
            type(e).__name__,
            str(e),
            exc_info=True,
        )
        traceback.print_exc()

        # Network failures to Supabase are surfaced distinctly (and map to 502)
        # so callers can distinguish "service down" from "bad credentials".
        if isinstance(e, httpx.HTTPError):
            raise _auth_http_error(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code="NETWORK_ERROR",
                message="Authentication service is temporarily unavailable. Please try again.",
                meta={"error_type": type(e).__name__},
            )

        # AuthApiError (wrong password, unconfirmed email, etc.) must NOT leak
        # the raw Supabase message to the client. The friendly message is set as
        # detail so the global handler surfaces it as `message`; the original
        # error type is kept only in meta for server-side debugging.
        if isinstance(e, AuthApiError):
            raise _auth_http_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="INVALID_CREDENTIALS",
                message="Invalid email or password.",
                meta={"error_type": type(e).__name__},
            )

        raise _auth_http_error(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTH_FAILED",
            message="Login failed. Please check your credentials and try again.",
            meta={"error_type": type(e).__name__},
        )

    if not response or not response.user:
        raise _auth_http_error(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTH_FAILED",
            message="Supabase login failed: user not returned",
        )

    token = create_access_token(
        subject=str(response.user.id),
        extra_data={"email": response.user.email},
    )

    return TokenResponse(access_token=token)


# ─────────────────────────────────────────────────────────────
# ME
# ─────────────────────────────────────────────────────────────
@router.get("/me", response_model=UserResponse)
async def me(current_user: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    return UserResponse(
        id=current_user["sub"],
        email=current_user["email"]
    )


# ─────────────────────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────────────────────
@router.post("/logout")
async def logout(current_user: dict[str, Any] = Depends(get_current_user)) -> dict:
    return {"message": "Logged out successfully"}
