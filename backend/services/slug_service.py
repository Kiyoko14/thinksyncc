import re
import secrets
import string
from typing import Any

from fastapi import HTTPException, status


class SlugService:
    """Generate safe, unique slugs for workspaces."""

    _VALID_SLUG_CHARS = set(string.ascii_lowercase + string.digits + "-")
    _MAX_SLUG_LENGTH = 30
    _SHORT_ID_LENGTH = 4

    @staticmethod
    def sanitize_name(name: str) -> str:
        """Sanitize workspace name for slug generation."""
        cleaned = name.strip().lower()

        # Replace spaces with hyphens
        cleaned = re.sub(r"\s+", "-", cleaned)

        # Remove invalid characters (keep only lowercase letters, digits, hyphens)
        cleaned = "".join(c if c in SlugService._VALID_SLUG_CHARS else "" for c in cleaned)

        # Strip leading/trailing hyphens
        cleaned = cleaned.strip("-")

        # Ensure not empty
        if not cleaned:
            cleaned = "workspace"

        # Truncate to reasonable length (leaving room for suffix)
        max_name_part = SlugService._MAX_SLUG_LENGTH - SlugService._SHORT_ID_LENGTH - 1
        cleaned = cleaned[:max_name_part]

        return cleaned

    @staticmethod
    def generate_short_id() -> str:
        """Generate a short unique identifier (4 chars)."""
        return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(SlugService._SHORT_ID_LENGTH))

    @staticmethod
    def generate_slug(name: str) -> str:
        """Generate a unique slug from workspace name."""
        sanitized = SlugService.sanitize_name(name)
        short_id = SlugService.generate_short_id()
        slug = f"{sanitized}-{short_id}"

        if len(slug) > SlugService._MAX_SLUG_LENGTH:
            slug = slug[: SlugService._MAX_SLUG_LENGTH]

        return slug

    @staticmethod
    def generate_domain(slug: str, base_domain: str = "app.yoursite.com") -> str:
        """Generate subdomain from slug."""
        if not slug or not all(c in SlugService._VALID_SLUG_CHARS for c in slug):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid slug format",
            )
        return f"https://{slug}.{base_domain}"
