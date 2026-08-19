"""
Repository Index — Sprint 3C.E (Context Engineering).

EXTENSION ONLY. Builds on the *existing* repository index already maintained
by ``services.context_engine`` (the ``workspace_files`` Supabase table). This
module adds lightweight engineering knowledge (module responsibilities,
dependencies, entry points, services, public APIs, DB models, workflows) on
top of that index and refreshes ONLY changed files — never rescans the whole
repository unnecessarily.

Reused:
    - ``ContextEngine._index_workspace_files`` (index scan + persistence)
    - ``workspace_files`` table (single source of truth)
    - ``RedisService`` (hot metadata cache)
No parallel index implementation is introduced.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from core.database import get_supabase, get_supabase_async
from services.context_engine import ContextEngine
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

_INDEX_TABLE = "workspace_files"
_META_TABLE = "repository_index_meta"
_CACHE_PREFIX = "repo_index"


@dataclass
class FileKnowledge:
    """Lightweight engineering knowledge for a single repository file."""

    path: str
    language: str = "unknown"
    size: int = 0
    last_modified: str = ""
    responsibility: str = ""
    symbols: list[str] = field(default_factory=list)  # public classes/functions
    depends_on: list[str] = field(default_factory=list)  # imported local modules
    is_entry_point: bool = False
    is_service: bool = False
    is_db_model: bool = False
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "size": self.size,
            "last_modified": self.last_modified,
            "responsibility": self.responsibility,
            "symbols": self.symbols,
            "depends_on": self.depends_on,
            "is_entry_point": self.is_entry_point,
            "is_service": self.is_service,
            "is_db_model": self.is_db_model,
            "content_hash": self.content_hash,
        }


class RepositoryIndex:
    """Incremental repository knowledge on top of the existing index.

    Refresh strategy: compare current file list/hashes against the cached
    metadata; re-analyse only files whose ``last_modified`` or hash changed.
    """

    def __init__(self) -> None:
        self._knowledge: dict[str, FileKnowledge] = {}

    # -- reuse existing index scan ----------------------------------------- #

    async def refresh(
        self,
        *,
        workspace_id: str,
        server: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        """Refresh repository knowledge. Returns change summary."""
        indexed = await ContextEngine._index_workspace_files(
            workspace_id=workspace_id,
            server=server,
            workspace_path=workspace_path,
        )
        previous = await self._load_meta(workspace_id)
        changed: list[str] = []
        for row in indexed:
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            new_hash = self._hash_row(row)
            old = previous.get(path)
            if old and old.get("content_hash") == new_hash:
                # Unchanged — reuse cached knowledge.
                self._knowledge[path] = FileKnowledge(**old)
                continue
            # Changed or new — fetch + analyse.
            fk = await self._analyse(row, server, workspace_path)
            if fk is not None:
                self._knowledge[path] = fk
                changed.append(path)
        # Persist meta (only the lightweight knowledge map).
        await self._persist_meta(workspace_id, {p: fk.to_dict() for p, fk in self._knowledge.items()})
        return {
            "total_files": len(self._knowledge),
            "changed_files": changed,
            "scanned_full": False,  # incremental by design
        }

    # -- analysis (local, cached by hash) ---------------------------------- #

    async def _analyse(
        self,
        row: dict[str, Any],
        server: dict[str, Any],
        workspace_path: str,
    ) -> FileKnowledge | None:
        from services.tools import read_workspace_file

        path = str(row.get("path") or "").strip()
        fk = FileKnowledge(
            path=path,
            language=str(row.get("language") or "unknown"),
            size=int(row.get("size") or 0),
            last_modified=str(row.get("last_modified") or ""),
        )
        if not path.endswith(".py"):
            fk.content_hash = self._hash_row(row)
            return fk
        res = await read_workspace_file(server=server, workspace_path=workspace_path, path=path, timeout=20)
        if res.get("code") != 0:
            fk.content_hash = self._hash_row(row)
            return fk
        content = res.get("stdout") or ""
        fk.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return fk
        local_modules = _local_imports(tree, base_dir=_dir(path))
        fk.depends_on = local_modules
        fk.symbols = [
            getattr(n, "name", "") for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        fk.is_entry_point = path in {"main.py", "app.py", "run.py", "__init__.py"} or bool(
            re_search(r"\b(if __name__ == .__main__.\s*:)", content)
        )
        fk.is_service = "service" in path.lower() or _has_class(tree, suffix="Service")
        fk.is_db_model = "model" in path.lower() or _has_class(tree, suffix="Base")
        return fk

    # -- persistence (Supabase + Redis hot cache) -------------------------- #

    async def _load_meta(self, workspace_id: str) -> dict[str, dict[str, Any]]:
        redis = RedisService.get_async_client()
        if redis is not None:
            try:
                raw = await redis.get(f"{_CACHE_PREFIX}:{workspace_id}")
                if raw:
                    import json
                    return json.loads(raw)
            except Exception:  # noqa: BLE001 — best-effort
                pass
        try:
            result = (
                await (await get_supabase_async())
                .table(_META_TABLE)
                .select("workspace_id,payload")
                .eq("workspace_id", workspace_id)
                .limit(1)
                .execute()
            )
            if result.data:
                payload = result.data[0].get("payload") or {}
                return payload if isinstance(payload, dict) else {}
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("repo_index meta load failed: %s", exc)
        return {}

    async def _persist_meta(self, workspace_id: str, payload: dict[str, dict[str, Any]]) -> None:
        import json
        redis = RedisService.get_async_client()
        if redis is not None:
            try:
                await redis.setex(f"{_CACHE_PREFIX}:{workspace_id}", 60 * 60 * 24, json.dumps(payload))
            except Exception:  # noqa: BLE001 — best-effort
                pass
        try:
            await (await get_supabase_async()).table(_META_TABLE).upsert(
                {"workspace_id": workspace_id, "payload": payload},
                on_conflict="workspace_id",
            ).execute()
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("repo_index meta persist failed: %s", exc)

    # -- queries ------------------------------------------------------------ #

    def entry_points(self) -> list[str]:
        return [p for p, fk in self._knowledge.items() if fk.is_entry_point]

    def services(self) -> list[str]:
        return [p for p, fk in self._knowledge.items() if fk.is_service]

    def db_models(self) -> list[str]:
        return [p for p, fk in self._knowledge.items() if fk.is_db_model]

    def responsibilities(self) -> dict[str, str]:
        return {p: fk.responsibility for p, fk in self._knowledge.items()}

    def relevant_to(self, *, task: str) -> list[str]:
        """Return paths most relevant to a task (keyword overlap on symbols)."""
        lowered = (task or "").lower()
        scored: list[tuple[int, str]] = []
        for path, fk in self._knowledge.items():
            score = 0
            if any(tok in lowered for tok in fk.symbols):
                score += 5
            if path.lower() in lowered or _base(path).lower() in lowered:
                score += 10
            if score:
                scored.append((score, path))
        scored.sort(key=lambda x: -x[0])
        return [p for _, p in scored]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _hash_row(row: dict[str, Any]) -> str:
    return hashlib.sha256(
        f"{row.get('path')}:{row.get('size')}:{row.get('last_modified')}".encode("utf-8")
    ).hexdigest()


def _dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _has_class(tree: ast.AST, suffix: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith(suffix):
            return True
    return False


def _local_imports(tree: ast.AST, *, base_dir: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level >= 1:
            out.append(f"{base_dir}/{node.module}".replace("//", "/"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("."):
                    out.append(f"{base_dir}/{alias.name.lstrip('.')}".replace("//", "/"))
    return out


def re_search(pattern: str, text: str) -> bool:
    import re
    return re.search(pattern, text) is not None


__all__ = ["RepositoryIndex", "FileKnowledge"]
