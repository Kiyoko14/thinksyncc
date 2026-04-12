import re
import string
from fastapi import HTTPException, status


class SlugService:
    """Slug + deploy domain helpers.

    Slugs are derived from the user-provided workspace name and must be:
    - lowercase
    - alphanumeric + hyphens
    Uniqueness is enforced per-server at the DB layer (and validated by the service).
    """

    _VALID_SLUG_CHARS = set(string.ascii_lowercase + string.digits + "-")
    _MAX_SLUG_LENGTH = 50

    @staticmethod
    def sanitize_name(name: str) -> str:
        """Sanitize workspace name for slug generation (no uniqueness)."""
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

        cleaned = cleaned[: SlugService._MAX_SLUG_LENGTH].strip("-") or "workspace"

        return cleaned

    @staticmethod
    def generate_slug(name: str) -> str:
        """Generate a deterministic slug from workspace name (no uniqueness)."""
        return SlugService.sanitize_name(name)

    @staticmethod
    def generate_domain(*, slug: str, workspace_id: str, base_domain: str = "thinksync.art") -> str:
        """Generate deploy domain using {slug}-{short_id}.{base_domain}."""
        if not slug or not all(c in SlugService._VALID_SLUG_CHARS for c in slug):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid slug format",
            )
        short_id = (workspace_id or "").replace("-", "").lower()[:6] or "000000"
        return f"{slug}-{short_id}.{base_domain}"
