"""ThinkSync authentication routes.

ThinkSync uses Google OAuth as the *only* authentication method. The browser
obtains a Google ID token and posts it to ``POST /auth/google``; the backend
verifies it cryptographically and issues a ThinkSync JWT. Email/password auth
(and the Supabase Auth dependency that powered it) was removed — see the
migration notes in SPRINT_OAUTH_MIGRATION_PLAN.md.

Routes:
  * ``POST /auth/google`` — exchange a verified Google ID token for a JWT.
  * ``GET  /auth/me``     — current user (protected).
  * ``POST /auth/logout`` — client-side logout signal (protected; JWT is stateless
    so the real invalidation happens client-side by dropping the token).
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from core.security import create_access_token, get_current_user
from models.user import GoogleLoginRequest, TokenResponse, UserResponse
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
async def google_login(payload: GoogleLoginRequest) -> TokenResponse:
    """Exchange a verified Google ID token for a ThinkSync JWT.

    The backend verifies the token cryptographically against Google's public
    certs, upserts the user into ``public.users`` (creating on first login,
    refreshing ``last_login_at`` on return), and issues a ThinkSync HS256 JWT
    whose ``sub`` is the ``public.users.id``. No password is ever involved.
    """
    try:
        claims = verify_google_id_token(payload.id_token)
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
# ME
# ─────────────────────────────────────────────────────────────
@router.get("/me", response_model=UserResponse)
async def me(current_user: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    return UserResponse(
        id=current_user["sub"],
        email=current_user["email"],
    )


# ─────────────────────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────────────────────
@router.post("/logout")
async def logout(current_user: dict[str, Any] = Depends(get_current_user)) -> dict:
    return {"message": "Logged out successfully"}
