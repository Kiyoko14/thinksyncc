from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from core.database import get_supabase
from core.security import create_access_token, get_current_user
from models.user import LoginRequest, RegisterRequest, TokenResponse, UserResponse

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


# ─────────────────────────────────────────────────────────────
# REGISTER
# ─────────────────────────────────────────────────────────────
@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest) -> TokenResponse:
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

        code = "AUTH_FAILED"
        http_status = status.HTTP_400_BAD_REQUEST
        if isinstance(e, httpx.HTTPError):
            code = "NETWORK_ERROR"
            http_status = status.HTTP_502_BAD_GATEWAY

        raise _auth_http_error(
            status_code=http_status,
            code=code,
            message=str(e) or "Supabase registration failed",
            meta={"error_type": type(e).__name__},
        )

    # ❗ Supabase ba’zida user qaytarmaydi (email confirmation yoqilgan bo‘lsa)
    if not response or not response.user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed: user not returned (email confirmation may be required)"
        )

    # ✅ JWT token yaratish
    token = create_access_token(
        subject=str(response.user.id),
        extra_data={"email": response.user.email},
    )

    return TokenResponse(access_token=token)


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

        code = "AUTH_FAILED"
        http_status = status.HTTP_401_UNAUTHORIZED
        if isinstance(e, httpx.HTTPError):
            code = "NETWORK_ERROR"
            http_status = status.HTTP_502_BAD_GATEWAY

        raise _auth_http_error(
            status_code=http_status,
            code=code,
            message=str(e) or "Supabase login failed",
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
