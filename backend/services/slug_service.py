import random
import re
import string
from fastapi import HTTPException, status


_NORMALIZED_NAME_RE = re.compile(r"^[a-z0-9]+$")
_RANDOM_SLUG_ALPHABET = string.ascii_lowercase + string.digits
_RANDOM_SLUG_LENGTH = 6
_MAX_NAME_LENGTH = 10
_MAX_SUBDOMAIN_LENGTH = 63


def normalize_name(name: str) -> str:
    """Strict workspace name validator. Lowercase alphanumeric, max 10 chars."""
    cleaned = (name or "").strip().lower()
    if not _NORMALIZED_NAME_RE.match(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only letters and numbers allowed",
        )
    if len(cleaned) > _MAX_NAME_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Max 10 characters",
        )
    return cleaned


def generate_random_slug() -> str:
    """Generate a 6-char [a-z0-9] random slug (uniqueness checked by caller)."""
    return "".join(random.choices(_RANDOM_SLUG_ALPHABET, k=_RANDOM_SLUG_LENGTH))


def build_subdomain(normalized_name: str, slug: str) -> str:
    """Build {name}-{slug} subdomain and validate total length ≤ 63."""
    subdomain = f"{normalized_name}-{slug}"
    if len(subdomain) > _MAX_SUBDOMAIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Subdomain too long",
        )
    return subdomain


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
