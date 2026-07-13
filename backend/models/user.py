from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)


class GoogleLoginRequest(BaseModel):
    """Body for ``POST /auth/google``.

    The browser obtains a Google-signed OIDC ID token (after the user clicks
    "Continue with Google") and posts it here. The backend verifies it directly
    against Google's public certs and never sees the user's password.
    """

    id_token: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterResponse(BaseModel):
    """Response for registration.

    When email confirmation is enabled in Supabase, ``sign_up`` returns a user
    but no session, so we cannot mint a backend JWT yet. In that case we set
    ``requires_confirmation=True`` and omit ``access_token`` instead of handing
    the client a phantom, unusable session.

    This model is additive: existing callers that only read ``access_token``
    still work for the confirmed path, and the new ``requires_confirmation``
    flag is opt-in to read.
    """

    access_token: str | None = None
    token_type: str = "bearer"
    requires_confirmation: bool = False


class UserResponse(BaseModel):
    id: str
    email: str
