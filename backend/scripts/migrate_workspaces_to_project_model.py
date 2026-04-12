from __future__ import annotations

import argparse
import asyncio
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass

from core.database import get_supabase
from services.server_service import ServerService
from services.ssh_service import SSHService


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EXTRACT_FROM_LEGACY_PATH = re.compile(r"/workspaces/(?:[0-9a-f-]{36}-)?(?P<slug>[a-z0-9-]+)$", re.IGNORECASE)


def _slugify(name: str) -> str:
    cleaned = (name or "").strip().lower().replace("_", "-")
    cleaned = re.sub(r"[^a-z0-9-]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "workspace"


def _short_id_from_uuid(uuid_text: str, length: int = 6) -> str:
    compact = (uuid_text or "").replace("-", "").lower()
    return (compact[:length] or "000000").lower()


def _derive_base_slug(*, existing_slug: str | None, name: str, path: str | None) -> str:
    if existing_slug and _SLUG_RE.match(existing_slug):
        return existing_slug

    if path:
        match = _EXTRACT_FROM_LEGACY_PATH.search(path.strip())
        if match:
            candidate = (match.group("slug") or "").lower()
            if _SLUG_RE.match(candidate):
                return candidate

        basename = path.strip().rstrip("/").split("/")[-1].lower()
        if _SLUG_RE.match(basename):
            return basename

    return _slugify(name)


def _ensure_unique_slug(*, server_id: str, base_slug: str, used: set[str]) -> str:
    candidate = base_slug
    if candidate not in used:
        used.add(candidate)
        return candidate

    i = 2
    while True:
        candidate = f"{base_slug}-{i}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


def _project_path(slug: str) -> str:
    return f"/home/root/workspaces/{slug}"


def _deploy_domain(*, slug: str, short_id: str) -> str:
    return f"{slug}-{short_id}.thinksync.art"


@dataclass(frozen=True)
class WorkspaceRow:
    id: str
    user_id: str
    server_id: str
    name: str
    path: str | None
    slug: str | None
    domain: str | None


async def _migrate(*, dry_run: bool, mkdir: bool) -> None:
    supabase = get_supabase()
    rows = (
        supabase.table("workspaces")
        .select("id,user_id,server_id,name,path,slug,domain")
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    workspaces = [
        WorkspaceRow(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            server_id=str(row["server_id"]),
            name=str(row.get("name") or ""),
            path=(row.get("path") or None),
            slug=(row.get("slug") or None),
            domain=(row.get("domain") or None),
        )
        for row in rows
    ]

    used_by_server: dict[str, set[str]] = defaultdict(set)
    for ws in workspaces:
        if ws.slug and _SLUG_RE.match(ws.slug):
            used_by_server[ws.server_id].add(ws.slug)

    for ws in workspaces:
        base_slug = _derive_base_slug(existing_slug=ws.slug, name=ws.name, path=ws.path)
        new_slug = _ensure_unique_slug(server_id=ws.server_id, base_slug=base_slug, used=used_by_server[ws.server_id])
        new_path = _project_path(new_slug)
        new_domain = _deploy_domain(slug=new_slug, short_id=_short_id_from_uuid(ws.id))

        patch: dict[str, str] = {}
        if ws.slug != new_slug:
            patch["slug"] = new_slug
        if ws.path != new_path:
            patch["path"] = new_path
        if (ws.domain or "") != new_domain:
            patch["domain"] = new_domain

        if not patch:
            continue

        if mkdir:
            server = ServerService.get_server(server_id=ws.server_id, user_id=ws.user_id)
            command = f"mkdir -p {shlex.quote(new_path)}"
            if not dry_run:
                await SSHService.execute(server=server, command=command)

        if dry_run:
            continue

        supabase.table("workspaces").update(patch).eq("id", ws.id).execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate existing workspaces to slug-based project model.")
    parser.add_argument("--dry-run", action="store_true", help="Compute changes but do not write to DB or SSH.")
    parser.add_argument("--no-mkdir", action="store_true", help="Do not create workspace directories on servers.")
    args = parser.parse_args()

    asyncio.run(_migrate(dry_run=bool(args.dry_run), mkdir=not bool(args.no_mkdir)))


if __name__ == "__main__":
    main()

