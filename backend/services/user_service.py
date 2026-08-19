"""ThinkSync identity store backed by the ``public.users`` table.

This is the canonical identity source after the Google OAuth migration. The
backend owns all identities; Supabase Auth is no longer used. Each user has a
stable ``public.users.id`` (UUID) that becomes the JWT ``sub`` and the FK target
for servers/workspaces/jobs/etc.

A Google login upserts by ``google_sub`` (insert on first login, refresh
``last_login_at`` + profile fields on return). The same table is reused for
future OAuth providers via provider-specific unique columns (e.g. github_sub).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from supabase import Client

from core.database import get_supabase

logger = logging.getLogger(__name__)


class UserService:
    TABLE = "public.users"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def upsert_from_google(cls, claims: dict[str, Any]) -> dict[str, Any]:
        """Find-or-create a ThinkSync user from verified Google claims.

        Matches on ``google_sub``. On first sign-in inserts a row; on return
        updates ``last_login_at`` (and profile fields if changed).
        """
        google_sub = claims.get("sub")
        if not google_sub:
            raise ValueError("Google claims missing 'sub'.")

        supabase: Client = get_supabase()
        existing = (
            supabase.table(cls.TABLE)
            .select("*")
            .eq("google_sub", google_sub)
            .maybe_single()
            .execute()
        )

        now = cls._now_iso()
        if existing and existing.data:
            user_id = existing.data["id"]
            supabase.table(cls.TABLE).update(
                {
                    "email": claims.get("email", existing.data.get("email")),
                    "display_name": claims.get("name"),
                    "avatar_url": claims.get("picture"),
                    "last_login_at": now,
                    "is_active": True,
                }
            ).eq("id", user_id).execute()
            row = dict(existing.data)
            row["last_login_at"] = now
            row["display_name"] = claims.get("name")
            row["avatar_url"] = claims.get("picture")
            return row

        insert_payload = {
            "email": claims.get("email"),
            "google_sub": google_sub,
            "display_name": claims.get("name"),
            "avatar_url": claims.get("picture"),
            "provider": "google",
            "is_active": True,
            "last_login_at": now,
        }
        created = supabase.table(cls.TABLE).insert(insert_payload).execute()
        if not created.data:
            raise RuntimeError("Failed to persist Google user to public.users.")
        return created.data[0]

    @classmethod
    def get_by_id(cls, user_id: str) -> dict[str, Any] | None:
        supabase: Client = get_supabase()
        result = (
            supabase.table(cls.TABLE)
            .select("id, email, display_name, avatar_url, provider, is_active")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        return result.data if result is not None else None
