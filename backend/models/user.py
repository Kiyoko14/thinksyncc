from pydantic import BaseModel, EmailStr, Field


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


class UserResponse(BaseModel):
    id: str
    email: str
