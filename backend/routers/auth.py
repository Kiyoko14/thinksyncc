from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from core.database import get_supabase
from core.security import create_access_token, get_current_user
from models.user import LoginRequest, RegisterRequest, TokenResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest) -> TokenResponse:
    supabase = get_supabase()
    try:
        response = supabase.auth.sign_up(
            {"email": payload.email, "password": payload.password}
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed",
        )

    if not response.user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed",
        )

    token = create_access_token(
        subject=str(response.user.id),
        extra_data={"email": response.user.email},
    )
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    supabase = get_supabase()
    try:
        response = supabase.auth.sign_in_with_password(
            {"email": payload.email, "password": payload.password}
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    if not response.user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    token = create_access_token(
        subject=str(response.user.id),
        extra_data={"email": response.user.email},
    )
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
async def me(current_user: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    return UserResponse(id=current_user["sub"], email=current_user["email"])


@router.post("/logout")
async def logout(current_user: dict[str, Any] = Depends(get_current_user)) -> dict:
    # JWT is stateless — invalidation is handled client-side by discarding the token.
    # Future: implement a token deny-list in Redis.
    return {"message": "Logged out successfully"}
