import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from core.config import get_settings

_ENCRYPTION_PREFIX = "enc:v1:"


def _get_fernet() -> Fernet:
    settings = get_settings()
    configured_key = (settings.DATA_ENCRYPTION_KEY or "").strip()

    if configured_key:
        key = configured_key.encode("utf-8")
    else:
        # Backward-compatible fallback if no dedicated key is configured.
        digest = hashlib.sha256(settings.JWT_SECRET.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)

    try:
        return Fernet(key)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid encryption configuration",
        )


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return value

    if value.startswith(_ENCRYPTION_PREFIX):
        return value

    token = _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{_ENCRYPTION_PREFIX}{token}"


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return value

    if not value.startswith(_ENCRYPTION_PREFIX):
        # Backward compatibility for existing plaintext rows.
        return value

    encrypted_token = value[len(_ENCRYPTION_PREFIX) :]
    try:
        return _get_fernet().decrypt(encrypted_token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt server credentials",
        )