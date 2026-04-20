from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from services.redis_service import RedisService
from services.tools import exec_in_workspace, read_workspace_file

logger = logging.getLogger(__name__)

_INDEX_TABLE = "workspace_files"
_LOG_TABLE = "agent_context_logs"
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".next",
    "dist",
    "build",
    "coverage",
}
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "your",
    "have",
    "need",
    "make",
    "build",
    "create",
    "write",
    "update",
    "patch",
    "code",
    "file",
    "files",
    "project",
    "workspace",
    "agent",
    "context",
    "system",
    "mode",
    "user",
    "task",
}
_LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".sql": "sql",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sh": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".env": "dotenv",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
}


@dataclass
class _SnippetWindow:
    start: int
    end: int
    score: int


class ContextEngine:
    @staticmethod
    async def build_context(
        *,
        workspace_id: str,
        task: str,
        server: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        task_hash = hashlib.sha256((task or "").strip().encode("utf-8")).hexdigest()
        cache_key = f"context:{workspace_id}:{task_hash}"
        cached = await ContextEngine._cache_get(cache_key)
        if cached is not None:
            ContextEngine._log_context_async(workspace_id=workspace_id, task=task, payload=cached, source="redis")
            return cached

        indexed_files = await ContextEngine._index_workspace_files(
            workspace_id=workspace_id,
            server=server,
            workspace_path=workspace_path,
        )
        selected = ContextEngine._select_files(task=task, indexed_files=indexed_files)
        snippets = await ContextEngine._extract_snippets(
            selected_files=selected,
            task=task,
            server=server,
            workspace_path=workspace_path,
        )
        mode = ContextEngine._detect_mode(task=task, indexed_files=indexed_files, selected_files=selected)
        payload = ContextEngine._build_payload(task=task, mode=mode, selected_files=selected, snippets=snippets)
        await ContextEngine._cache_set(cache_key, payload)
        await ContextEngine._log_context(workspace_id=workspace_id, task=task, payload=payload, source="fresh")
        return payload

    @staticmethod
    async def _index_workspace_files(
        *,
        workspace_id: str,
        server: dict[str, Any],
        workspace_path: str,
    ) -> list[dict[str, Any]]:
        settings = get_settings()
        max_files = max(100, int(settings.AGENT_CONTEXT_MAX_INDEXED_FILES))
        script = textwrap.dedent(
            f"""
            import json
            import os
            from datetime import datetime, timezone

            skip = {sorted(_SKIP_DIRS)!r}
            max_files = {max_files}
            language_by_extension = {json.dumps(_LANGUAGE_BY_EXTENSION)}
            rows = []
            seen = 0
            for root, dirs, files in os.walk(".", topdown=True):
                dirs[:] = [d for d in dirs if d not in skip]
                for name in files:
                    rel = os.path.normpath(os.path.join(root, name)).replace("\\\\", "/")
                    if rel.startswith("./"):
                        rel = rel[2:]
                    try:
                        stat = os.stat(rel)
                    except OSError:
                        continue
                    _, ext = os.path.splitext(rel.lower())
                    rows.append({{
                        "workspace_id": {workspace_id!r},
                        "path": rel,
                        "size": int(stat.st_size),
                        "last_modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        "language": language_by_extension.get(ext, "unknown"),
                    }})
                    seen += 1
                    if seen >= max_files:
                        break
                if seen >= max_files:
                    break
            print(json.dumps(rows, ensure_ascii=False))
            """
        ).strip()
        result = await exec_in_workspace(
            server=server,
            workspace_path=workspace_path,
            command=f"python3 -c {shlex.quote(script)}",
            timeout=30,
        )
        if result["code"] != 0:
            logger.warning("Context index scan failed for workspace=%s: %s", workspace_id, result["stderr"] or result["stdout"])
            return await ContextEngine._load_index_from_supabase(workspace_id)
        try:
            rows = json.loads(result["stdout"] or "[]")
        except json.JSONDecodeError:
            logger.warning("Context index returned invalid JSON for workspace=%s", workspace_id)
            return await ContextEngine._load_index_from_supabase(workspace_id)
        normalized = [row for row in rows if isinstance(row, dict) and str(row.get("path") or "").strip()]
        await ContextEngine._persist_index(rows=normalized)
        return normalized

    @staticmethod
    async def _load_index_from_supabase(workspace_id: str) -> list[dict[str, Any]]:
        try:
            result = (
                get_supabase()
                .table(_INDEX_TABLE)
                .select("workspace_id,path,size,last_modified,language")
                .eq("workspace_id", workspace_id)
                .limit(get_settings().AGENT_CONTEXT_MAX_INDEXED_FILES)
                .execute()
            )
            return list(result.data or [])
        except Exception as exc:
            logger.warning("Failed to load workspace index from Supabase: %s", exc)
            return []

    @staticmethod
    async def _persist_index(*, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            get_supabase().table(_INDEX_TABLE).upsert(rows, on_conflict="workspace_id,path").execute()
        except APIError as exc:
            logger.warning("Failed to persist workspace index: %s", exc)

    @staticmethod
    def _extract_keywords(task: str) -> tuple[set[str], set[str]]:
        lowered = (task or "").lower()
        keywords = {
            token
            for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", lowered)
            if token not in _STOPWORDS
        }
        path_mentions = {
            token.strip("./").lower()
            for token in re.findall(r"[\w./-]+\.[A-Za-z0-9]+", task or "")
            if "/" in token or "." in token
        }
        return keywords, path_mentions

    @staticmethod
    def _select_files(*, task: str, indexed_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        max_files = max(1, int(get_settings().AGENT_CONTEXT_MAX_FILES))
        keywords, path_mentions = ContextEngine._extract_keywords(task)
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in indexed_files:
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            lowered_path = path.lower()
            base = os.path.basename(lowered_path)
            score = 0
            if lowered_path in path_mentions or base in path_mentions:
                score += 20
            for keyword in keywords:
                if keyword == base:
                    score += 12
                elif keyword in lowered_path:
                    score += 4
            language = str(row.get("language") or "").strip().lower()
            if language and language in (task or "").lower():
                score += 2
            if language in {"python", "typescript", "javascript", "sql"}:
                score += 1
            if score > 0:
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], int(item[1].get("size") or 0), str(item[1].get("path") or "")))
        return [row for _, row in ranked[:max_files]]

    @staticmethod
    async def _extract_snippets(
        *,
        selected_files: list[dict[str, Any]],
        task: str,
        server: dict[str, Any],
        workspace_path: str,
    ) -> list[dict[str, Any]]:
        settings = get_settings()
        total_budget = max(120, int(settings.AGENT_CONTEXT_MAX_TOTAL_LINES))
        per_file_budget = max(40, int(settings.AGENT_CONTEXT_MAX_LINES_PER_FILE))
        keywords, _ = ContextEngine._extract_keywords(task)
        snippets: list[dict[str, Any]] = []
        remaining = total_budget
        for row in selected_files:
            if remaining <= 0:
                break
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            res = await read_workspace_file(server=server, workspace_path=workspace_path, path=path, timeout=20)
            if res["code"] != 0:
                continue
            content = res["stdout"] or ""
            extracted = ContextEngine._extract_file_snippet(
                path=path,
                content=content,
                keywords=keywords,
                line_budget=min(per_file_budget, remaining),
            )
            if extracted is None:
                continue
            snippets.append(extracted)
            remaining -= int(extracted.get("line_count") or 0)
        return snippets

    @staticmethod
    def _extract_file_snippet(
        *,
        path: str,
        content: str,
        keywords: set[str],
        line_budget: int,
    ) -> dict[str, Any] | None:
        lines = content.splitlines()
        if not lines:
            return None
        windows: list[_SnippetWindow] = []
        if path.lower().endswith(".py"):
            windows.extend(ContextEngine._python_windows(content=content, keywords=keywords))
        windows.extend(ContextEngine._keyword_windows(lines=lines, keywords=keywords))
        if not windows:
            windows = [_SnippetWindow(start=1, end=min(len(lines), max(1, min(line_budget, 20))), score=1)]
        selected = ContextEngine._select_unique_contiguous_range(
            lines=lines,
            content=content,
            windows=windows,
            max_lines=line_budget,
        )
        if selected is None:
            return None
        start, end = selected
        snippet = "\n".join(lines[start - 1:end])
        return {
            "path": path,
            "content": content,
            "snippet": snippet,
            "line_count": end - start + 1,
            "ranges": [{"start": start, "end": end}],
        }

    @staticmethod
    def _python_windows(*, content: str, keywords: set[str]) -> list[_SnippetWindow]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        windows: list[_SnippetWindow] = []
        lines = content.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            start = getattr(node, "lineno", 1)
            end = getattr(node, "end_lineno", start)
            name = getattr(node, "name", "").lower()
            body = "\n".join(lines[start - 1:end]).lower()
            score = 3
            if any(keyword in name for keyword in keywords):
                score += 10
            score += sum(2 for keyword in keywords if keyword in body)
            if score > 3:
                windows.append(_SnippetWindow(start=max(1, start - 1), end=end, score=score))
        return windows

    @staticmethod
    def _keyword_windows(*, lines: list[str], keywords: set[str]) -> list[_SnippetWindow]:
        windows: list[_SnippetWindow] = []
        for idx, line in enumerate(lines, start=1):
            lowered = line.lower()
            score = sum(2 for keyword in keywords if keyword in lowered)
            if re.search(r"\b(async\s+def|def|class|function|export\s+function|router\.|app\.)", lowered):
                score += 3
            if score > 0:
                windows.append(_SnippetWindow(start=max(1, idx - 6), end=min(len(lines), idx + 14), score=score))
        return windows

    @staticmethod
    def _select_unique_contiguous_range(
        *,
        lines: list[str],
        content: str,
        windows: list[_SnippetWindow],
        max_lines: int,
    ) -> tuple[int, int] | None:
        total_lines = len(lines)
        ordered = sorted(windows, key=lambda item: (-item.score, item.start, item.end))
        fallback: tuple[int, int] | None = None
        padding = 8

        for window in ordered:
            start = max(1, window.start - padding)
            end = min(total_lines, window.end + padding)
            if end - start + 1 > max_lines:
                end = min(total_lines, start + max_lines - 1)
            current_start = start
            current_end = end

            while True:
                snippet = "\n".join(lines[current_start - 1:current_end])
                if snippet:
                    count = content.count(snippet)
                    if count == 1:
                        return (current_start, current_end)
                    if fallback is None:
                        fallback = (current_start, current_end)
                current_len = current_end - current_start + 1
                can_expand_left = current_start > 1
                can_expand_right = current_end < total_lines
                if current_len >= max_lines or (not can_expand_left and not can_expand_right):
                    break
                if can_expand_left:
                    current_start -= 1
                if can_expand_right and (current_end - current_start + 1) < max_lines:
                    current_end += 1

        return fallback

    @staticmethod
    def _detect_mode(*, task: str, indexed_files: list[dict[str, Any]], selected_files: list[dict[str, Any]]) -> str:
        _, path_mentions = ContextEngine._extract_keywords(task)
        known_paths = {str(row.get("path") or "").strip().lower() for row in indexed_files}
        known_basenames = {os.path.basename(path) for path in known_paths}
        if any(mention in known_paths or os.path.basename(mention) in known_basenames for mention in path_mentions):
            return "PATCH"
        if selected_files:
            return "PATCH"
        return "CREATE"

    @staticmethod
    def _build_payload(
        *,
        task: str,
        mode: str,
        selected_files: list[dict[str, Any]],
        snippets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        file_list = [str(row.get("path") or "") for row in selected_files]
        code_snippets = {item["path"]: item["snippet"] for item in snippets if item.get("path") and item.get("snippet")}
        return {
            "mode": mode,
            "selected_files": file_list,
            "snippets": snippets,
            "prompt_payload": {
                "MODE": mode,
                "FILE_LIST": file_list,
                "CODE_SNIPPETS": code_snippets,
                "USER_TASK": task,
            },
        }

    @staticmethod
    async def _cache_get(key: str) -> dict[str, Any] | None:
        redis = RedisService.get_async_client()
        if redis is None:
            return None
        try:
            raw = await redis.get(key)
            if not raw:
                return None
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except Exception as exc:
            logger.warning("Failed to read context cache: %s", exc)
            return None

    @staticmethod
    async def _cache_set(key: str, payload: dict[str, Any]) -> None:
        redis = RedisService.get_async_client()
        if redis is None:
            return
        try:
            await redis.setex(key, get_settings().REDIS_CONTEXT_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            logger.warning("Failed to write context cache: %s", exc)

    @staticmethod
    async def _log_context(*, workspace_id: str, task: str, payload: dict[str, Any], source: str) -> None:
        try:
            get_supabase().table(_LOG_TABLE).insert(
                {
                    "workspace_id": workspace_id,
                    "task": task,
                    "selected_files": payload.get("selected_files") or [],
                    "snippet_preview": json.dumps(payload.get("prompt_payload") or {}, ensure_ascii=False)[:4000],
                    "source": source,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception as exc:
            logger.warning("Failed to write context log: %s", exc)

    @staticmethod
    def _log_context_async(*, workspace_id: str, task: str, payload: dict[str, Any], source: str) -> None:
        try:
            asyncio.create_task(
                ContextEngine._log_context(
                    workspace_id=workspace_id,
                    task=task,
                    payload=payload,
                    source=source,
                )
            )
        except Exception:
            pass
