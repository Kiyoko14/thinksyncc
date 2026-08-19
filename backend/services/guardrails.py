"""Guardrails validation pipeline (pre-execution).

This module validates generated or templated code before it reaches execution.
It is intentionally generic and does not assume any single product use-case.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import shlex
from typing import Any

from services.tools import exec_in_workspace

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 200 * 1024

_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("rm_rf_root", re.compile(r"(?is)\brm\s+-rf\s+/\s*(?:$|[;&|])")),
    ("shutdown", re.compile(r"(?is)\bshutdown\b")),
    ("reboot", re.compile(r"(?is)\breboot\b")),
    ("mkfs", re.compile(r"(?is)\bmkfs\b")),
    ("dd_if", re.compile(r"(?is)\bdd\s+if=")),
    ("etc_passwd", re.compile(r"(?is)/etc/passwd")),
]

_SUSPICIOUS_MODULES_BLOCK: set[str] = set()
_SUSPICIOUS_MODULES_WARN: set[str] = {
    "subprocess",
}

_OS_SYSTEM_RE = re.compile(r"(?is)\bos\.system\s*\(")

_IMPORT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

_FENCE_RE = re.compile(r"```(?:python)?\s*(.*?)```", flags=re.DOTALL | re.IGNORECASE)


def _strip_bad_chars(code: str) -> str:
    text = (code or "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _auto_fix_markdown_fences(code: str) -> str:
    text = code or ""
    if "```" not in text:
        return text
    matches = _FENCE_RE.findall(text)
    if not matches:
        return text.replace("```", "")
    joined = "\n\n".join(m.strip("\n") for m in matches if m and m.strip())
    return joined.strip() if joined.strip() else text.replace("```", "")


def _detect_infinite_recursion_heuristic(code: str) -> bool:
    # Heuristic: function immediately returns itself (common accidental recursion).
    return bool(re.search(r"(?is)^\s*def\s+([A-Za-z_]\w*)\s*\(.*\)\s*:\s*\n\s*return\s+\1\s*\(", code or "", flags=re.MULTILINE))


def _parse_imports(code: str) -> tuple[list[str], list[str]]:
    modules: set[str] = set()
    invalid: list[str] = []
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return ([], [])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = (alias.name or "").strip()
                if not name:
                    continue
                top = name.split(".", 1)[0]
                if not _IMPORT_NAME_RE.match(name):
                    invalid.append(name)
                    continue
                modules.add(top)
        elif isinstance(node, ast.ImportFrom):
            if getattr(node, "level", 0):
                # Relative import: treat as local; ignore.
                continue
            mod = (node.module or "").strip()
            if not mod:
                continue
            if not _IMPORT_NAME_RE.match(mod):
                invalid.append(mod)
                continue
            modules.add(mod.split(".", 1)[0])
    return (sorted(modules), sorted(set(invalid)))


def validate_workspace_path(workspace_path: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    path = (workspace_path or "").strip()
    if not path:
        errors.append("workspace_path is empty")
    if "\n" in path or "\r" in path:
        errors.append("workspace_path contains newline characters")
    if ".." in path:
        errors.append("workspace_path contains path traversal ('..')")
    if not path.startswith("/root/workspaces/"):
        errors.append("workspace_path must start with /root/workspaces/")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def validate_code(code: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    sanitized = _strip_bad_chars(code or "")
    sanitized = _auto_fix_markdown_fences(sanitized)
    sanitized = sanitized.strip()

    if not sanitized:
        errors.append("code is empty")

    if "```" in sanitized:
        errors.append("code contains markdown fences (```)")

    size = len(sanitized.encode("utf-8", errors="ignore"))
    if size > MAX_FILE_BYTES:
        errors.append(f"code exceeds size limit: {size} bytes > {MAX_FILE_BYTES} bytes")

    lowered = sanitized.lower()
    for name, pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(sanitized):
            errors.append(f"forbidden_pattern:{name}")

    if "asyncio.run(" in lowered and "run_polling" in lowered:
        errors.append("bad_pattern: asyncio.run(...) with run_polling()")

    if _detect_infinite_recursion_heuristic(sanitized):
        errors.append("bad_pattern: suspected infinite recursion")

    modules, invalid_imports = _parse_imports(sanitized)
    for name in invalid_imports:
        errors.append(f"invalid_import:{name}")

    for mod in modules:
        if mod in _SUSPICIOUS_MODULES_BLOCK:
            errors.append(f"suspicious_import_blocked:{mod}")
        elif mod in _SUSPICIOUS_MODULES_WARN:
            warnings.append(f"suspicious_import:{mod}")

    if _OS_SYSTEM_RE.search(sanitized):
        warnings.append("suspicious_call:os.system")

    valid = not errors
    return {"valid": valid, "errors": errors, "warnings": warnings, "sanitized_code": sanitized}


def _validate_relative_entrypoint(entrypoint: str) -> str:
    rel = (entrypoint or "").strip().lstrip("/")
    if not rel or ".." in rel or "\n" in rel or "\r" in rel:
        raise ValueError("invalid entrypoint path")
    return rel


async def validate_python_syntax(
    *,
    server: dict[str, Any],
    workspace_path: str,
    entrypoint: str = "main.py",
    timeout: int = 15,
) -> dict[str, Any]:
    rel = _validate_relative_entrypoint(entrypoint)
    res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m py_compile " + shlex.quote(rel),
        timeout=max(5, int(timeout)),
    )
    if res["code"] == 0:
        return {"valid": True, "errors": [], "warnings": []}
    msg = (res["stdout"] + "\n" + res["stderr"]).strip() or "py_compile failed"
    return {"valid": False, "errors": [msg], "warnings": []}


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _count_non_overlapping(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    return haystack.count(needle)


def _resolve_exact_or_trimmed_match(current: str, target: str) -> tuple[str, str, int]:
    exact_count = _count_non_overlapping(current, target)
    if exact_count == 1:
        return target, "exact", exact_count

    trimmed = target.strip()
    if trimmed and trimmed != target:
        trimmed_count = _count_non_overlapping(current, trimmed)
        if trimmed_count == 1:
            return trimmed, "trimmed_edges", trimmed_count

    if exact_count > 1:
        return target, "exact_ambiguous", exact_count
    if trimmed and trimmed != target and _count_non_overlapping(current, trimmed) > 1:
        return trimmed, "trimmed_edges_ambiguous", _count_non_overlapping(current, trimmed)
    return target, "none", exact_count


def apply_text_patches(
    *,
    existing_files: list[dict[str, Any]],
    patches: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Apply minimal string-based patches to an in-memory file set.

    Patch semantics:
    - replace: replace EXACT target snippet (must match exactly once) with replacement
    - delete: delete EXACT target snippet (must match exactly once)
    - insert: insert replacement immediately AFTER EXACT target snippet (must match exactly once)
    """
    files_by_path: dict[str, str] = {}
    for item in existing_files or []:
        if not isinstance(item, dict):
            continue
        path = _coerce_str(item.get("path")).strip()
        if not path:
            continue
        files_by_path[path] = _coerce_str(item.get("content"))

    errors: list[str] = []
    error_details: list[dict[str, Any]] = []
    changed: set[str] = set()

    for idx, patch in enumerate(patches or [], start=1):
        if not isinstance(patch, dict):
            errors.append(f"patch[{idx}]: invalid patch object")
            continue

        path = _coerce_str(patch.get("file")).strip()
        op = _coerce_str(patch.get("operation")).strip().lower()
        target = _coerce_str(patch.get("target"))
        replacement = _coerce_str(patch.get("replacement"))

        if not path:
            errors.append(f"patch[{idx}]: missing file")
            error_details.append({"patch_index": idx, "type": "missing_file", "path": path, "match_count": 0})
            continue
        if path not in files_by_path:
            errors.append(f"patch[{idx}]: file not found: {path}")
            error_details.append({"patch_index": idx, "type": "file_not_found", "path": path, "match_count": 0})
            continue
        if op not in {"replace", "insert", "delete"}:
            errors.append(f"patch[{idx}]: invalid operation: {op}")
            error_details.append({"patch_index": idx, "type": "invalid_operation", "path": path, "match_count": 0})
            continue
        if not target:
            errors.append(f"patch[{idx}]: empty target")
            error_details.append({"patch_index": idx, "type": "empty_target", "path": path, "match_count": 0})
            continue

        current = files_by_path[path]
        matched_target, match_mode, count = _resolve_exact_or_trimmed_match(current, target)
        logger.info(
            "[patch] file=%s match_mode=%s match_count=%s target=%r",
            path,
            match_mode,
            count,
            target[:400],
        )
        if count != 1:
            errors.append(f"patch[{idx}]: target mismatch in {path} (mode={match_mode}, count={count}, expected=1)")
            error_details.append(
                {
                    "patch_index": idx,
                    "type": "target_mismatch",
                    "path": path,
                    "match_mode": match_mode,
                    "match_count": count,
                    "target_preview": target[:400],
                }
            )
            continue

        updated = current
        if op == "replace":
            updated = current.replace(matched_target, replacement, 1)
        elif op == "delete":
            updated = current.replace(matched_target, "", 1)
        else:  # insert
            pos = current.find(matched_target)
            insert_at = pos + len(matched_target)
            updated = current[:insert_at] + replacement + current[insert_at:]

        if updated == current:
            errors.append(f"patch[{idx}]: patch did not change file content in {path}")
            error_details.append(
                {
                    "patch_index": idx,
                    "type": "no_change",
                    "path": path,
                    "match_mode": match_mode,
                    "match_count": count,
                    "target_preview": matched_target[:400],
                }
            )
            continue

        if op in {"replace", "delete"} and _count_non_overlapping(updated, matched_target) >= _count_non_overlapping(current, matched_target):
            errors.append(f"patch[{idx}]: change verification failed in {path}")
            error_details.append(
                {
                    "patch_index": idx,
                    "type": "change_verification_failed",
                    "path": path,
                    "match_mode": match_mode,
                    "match_count": count,
                    "target_preview": matched_target[:400],
                }
            )
            continue

        files_by_path[path] = updated
        changed.add(path)

    updated_files = [{"path": path, "content": content} for path, content in files_by_path.items()]
    return {
        "ok": not errors,
        "errors": errors,
        "error_details": error_details,
        "changed_files": sorted(changed),
        "updated_files": updated_files,
    }


def validate_patched_files(
    *,
    original_files: list[dict[str, Any]],
    updated_files: list[dict[str, Any]],
    patches: list[dict[str, Any]],
    checks: list[str] | None = None,
) -> dict[str, Any]:
    requested = set((checks or ["syntax_valid", "required_feature_present", "no_unintended_changes"]))
    errors: list[str] = []

    orig_by_path: dict[str, str] = {}
    for item in original_files or []:
        if isinstance(item, dict):
            p = _coerce_str(item.get("path")).strip()
            if p:
                orig_by_path[p] = _coerce_str(item.get("content"))

    upd_by_path: dict[str, str] = {}
    for item in updated_files or []:
        if isinstance(item, dict):
            p = _coerce_str(item.get("path")).strip()
            if p:
                upd_by_path[p] = _coerce_str(item.get("content"))

    patched_paths = {(_coerce_str(p.get("file")).strip()) for p in (patches or []) if isinstance(p, dict)}
    patched_paths.discard("")

    if "no_unintended_changes" in requested:
        for path, before in orig_by_path.items():
            after = upd_by_path.get(path)
            if after is None:
                errors.append(f"no_unintended_changes: missing updated file: {path}")
                continue
            if path not in patched_paths and after != before:
                errors.append(f"no_unintended_changes: file changed without a patch: {path}")

    if "required_feature_present" in requested:
        for idx, patch in enumerate(patches or [], start=1):
            if not isinstance(patch, dict):
                continue
            path = _coerce_str(patch.get("file")).strip()
            op = _coerce_str(patch.get("operation")).strip().lower()
            target = _coerce_str(patch.get("target"))
            replacement = _coerce_str(patch.get("replacement"))
            content = upd_by_path.get(path)
            if content is None:
                errors.append(f"required_feature_present: missing updated file: {path}")
                continue
            if op in {"replace", "insert"} and replacement and replacement not in content:
                errors.append(f"required_feature_present: patch[{idx}] replacement not found in {path}")
            if op == "delete" and target and target in content:
                errors.append(f"required_feature_present: patch[{idx}] target still present in {path}")

    if "syntax_valid" in requested:
        for path in sorted(patched_paths):
            content = upd_by_path.get(path)
            if content is None:
                continue
            if path.endswith(".py"):
                try:
                    ast.parse(content)
                except SyntaxError as exc:
                    errors.append(f"syntax_valid: {path}: {exc}")
            elif path.endswith(".json"):
                try:
                    json.loads(content)
                except Exception as exc:
                    errors.append(f"syntax_valid: {path}: invalid json: {exc}")

    return {"ok": not errors, "errors": errors}
