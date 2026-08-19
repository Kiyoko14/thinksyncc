from __future__ import annotations

import logging
import re
import shlex
import time
from typing import Any, Literal

from services import logger as obs
from services.tools import append_run_log, exec_in_workspace, universal_execute_python, write_workspace_file

logger = logging.getLogger(__name__)

MAX_RETRIES = 2

_MODULE_TO_PIP: dict[str, str] = {
    "telegram": "python-telegram-bot",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
}


def parse_error(logs: str) -> dict[str, Any]:
    text = logs or ""

    def _last_match(pattern: str, flags: int = 0) -> re.Match[str] | None:
        matches = list(re.finditer(pattern, text, flags))
        return matches[-1] if matches else None

    mm = _last_match(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]")
    if mm:
        module = (mm.group(1) or "").strip()
        module = module.split(".", 1)[0].strip() if module else ""
        return {"type": "ModuleNotFoundError", "message": mm.group(0).strip(), "module": module or None}

    im = _last_match(r"ImportError:\s+No module named ['\"]([^'\"]+)['\"]")
    if im:
        module = (im.group(1) or "").strip()
        module = module.split(".", 1)[0].strip() if module else ""
        return {"type": "ImportError", "message": im.group(0).strip(), "module": module or None}

    ic = _last_match(
        r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"](?:\s+\(([^)]+)\))?"
    )
    if ic:
        return {"type": "ImportError", "message": ic.group(0).strip(), "module": (ic.group(2) or "").strip() or None}

    se = _last_match(r"(?:SyntaxError|IndentationError):\s+.+")
    if se:
        return {"type": "SyntaxError", "message": se.group(0).strip(), "module": None}

    te = _last_match(r"\bTimeoutError\b.*")
    if te:
        return {"type": "TimeoutError", "message": te.group(0).strip(), "module": None}

    # Shell timeout (e.g. `timeout 30s python3 ...`)
    sto = _last_match(r"(?im)^\s*timeout:.*$")
    if sto:
        return {"type": "TimeoutError", "message": sto.group(0).strip(), "module": None}

    re_err = _last_match(r"RuntimeError:\s+.+")
    if re_err:
        return {"type": "RuntimeError", "message": re_err.group(0).strip(), "module": None}

    return {"type": "Unknown", "message": (text.strip()[-400:] if text.strip() else ""), "module": None}


def _clean_markdown_fences(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    pattern = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
    matches = pattern.findall(raw)
    if matches:
        cleaned = "\n\n".join(m.strip() for m in matches if (m or "").strip())
        return cleaned.strip()
    # Fallback: remove obvious fence lines
    lines = []
    for line in raw.splitlines():
        if line.strip().startswith("```"):
            continue
        lines.append(line)
    return "\n".join(lines).strip().strip("`").strip()


def _fix_telegram_filters_import(code: str) -> str:
    if "telegram" not in (code or ""):
        return code
    lines: list[str] = []
    changed = False
    for line in (code or "").splitlines():
        m = re.match(r"^(\s*from\s+telegram\.ext\s+import\s+)(.+?)\s*$", line)
        if not m:
            lines.append(line)
            continue
        prefix, imports = m.group(1), m.group(2)
        parts = [p.strip() for p in imports.split(",") if p.strip()]
        if not parts or "Filters" not in parts:
            lines.append(line)
            continue
        new_parts: list[str] = []
        for part in parts:
            if part == "Filters":
                if "filters" not in new_parts:
                    new_parts.append("filters")
                changed = True
            else:
                new_parts.append(part)
        # De-dupe while preserving order
        deduped: list[str] = []
        seen: set[str] = set()
        for part in new_parts:
            if part in seen:
                continue
            seen.add(part)
            deduped.append(part)
        lines.append(prefix + ", ".join(deduped))
    updated = "\n".join(lines)
    if "Filters." in updated:
        updated2 = re.sub(r"\bFilters\.", "filters.", updated)
        changed = changed or (updated2 != updated)
        updated = updated2
    return updated if changed else code


def _minimal_syntax_cleanup(code: str, logs: str) -> str:
    text = code or ""
    if not text:
        return text

    # Normalize common "smart quotes" that frequently break code pasted from rich text.
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\ufeff", "")
    )

    # Remove markdown fence lines.
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            continue
        lines.append(line)
    text = "\n".join(lines)

    # Remove NUL and other control chars (keep tab/newline/carriage return).
    text = "".join(ch for ch in text if (ch in "\n\t\r") or (ord(ch) >= 32))

    # If traceback has a syntax error line reference, only strip that line when it's clearly non-code.
    ln = None
    m = re.search(r'File\s+"[^"]+",\s+line\s+(\d+)', logs or "")
    if m:
        try:
            ln = int(m.group(1))
        except Exception:
            ln = None
    if ln is not None:
        parts = text.splitlines()
        idx = ln - 1
        if 0 <= idx < len(parts):
            suspect = parts[idx].strip()
            if suspect in {"python", "python3"} or suspect.startswith(("pip install", "python -m pip", "Traceback", "ERROR:", "Output:")):
                parts.pop(idx)
                text = "\n".join(parts)

    return text


async def _pip_install_missing(
    *,
    server: dict[str, Any],
    workspace_path: str,
    module: str,
    timeout: int,
) -> str:
    name = (module or "").strip()
    if not name:
        return ""
    pkg = _MODULE_TO_PIP.get(name, name)
    cmd = "python3 -m pip install " + shlex.quote(pkg)
    res = await exec_in_workspace(server=server, workspace_path=workspace_path, command=cmd, timeout=max(30, int(timeout)))
    logs = (res["stdout"] + res["stderr"]).strip()
    return logs


async def _run_background(
    *,
    server: dict[str, Any],
    workspace_path: str,
    entrypoint: str,
    code: str,
    timeout: int,
) -> dict[str, Any]:
    rel_entry = entrypoint.strip().lstrip("/") or "main.py"
    await write_workspace_file(
        server=server,
        workspace_path=workspace_path,
        path=rel_entry,
        content=code or "",
        allow_write=True,
        timeout=max(10, int(timeout)),
    )
    cmd = (
        "set -e; "
        ": > output.log; "
        "ln -sf output.log error.log; "
        f"nohup python3 {shlex.quote(rel_entry)} > output.log 2>&1 < /dev/null & "
        "echo $! > app.pid"
    )
    await exec_in_workspace(server=server, workspace_path=workspace_path, command=cmd, timeout=20)
    tail = (
        "echo '--- output.log (stdout+stderr) ---'; "
        "test -f output.log && tail -n 100 output.log || true"
    )
    res = await exec_in_workspace(server=server, workspace_path=workspace_path, command=tail, timeout=max(10, int(timeout)))
    return {
        "type": "background",
        "status": "running",
        "workspace": workspace_path,
        "log_file": "output.log",
        "logs": (res["stdout"] + res["stderr"]).strip(),
        "success": True,
    }


async def execute_with_self_healing(
    *,
    server: dict[str, Any],
    workspace_path: str,
    code: str,
    entrypoint: str = "main.py",
    setup_timeout: int = 300,
    job_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    trace_id = (trace_id or "").strip() or obs.new_trace_id()
    applied_fixes: list[str] = []
    used_fix_keys: set[str] = set()
    attempts = 0
    retries = 0
    current_code = code or ""
    next_mode: Literal["DEFAULT", "BACKGROUND"] = "DEFAULT"
    aggregated_fix_logs: list[str] = []
    total_t0 = time.perf_counter()
    execution_time = 0.0
    retry_time = 0.0

    while True:
        attempts += 1
        await append_run_log(
            server=server,
            workspace_path=workspace_path,
            entry=obs.make_log(
                level="INFO",
                layer="self_healing",
                message="attempt_start",
                trace_id=trace_id,
                meta={"job_id": job_id, "attempt": attempts, "retries": retries, "mode": next_mode},
            ),
            timeout=5,
        )

        exec_t0 = time.perf_counter()
        if next_mode == "BACKGROUND":
            result = await _run_background(
                server=server,
                workspace_path=workspace_path,
                entrypoint=entrypoint,
                code=current_code,
                timeout=setup_timeout,
            )
        else:
            result = await universal_execute_python(
                server=server,
                workspace_path=workspace_path,
                code=current_code,
                entrypoint=entrypoint,
                setup_timeout=setup_timeout,
                trace_id=trace_id,
            )
        execution_time += max(0.0, time.perf_counter() - exec_t0)

        logs = str(result.get("logs") or "")
        success = bool(result.get("success")) or (result.get("type") == "background" and result.get("status") == "running")
        if success:
            if aggregated_fix_logs:
                prefix = "\n\n".join(part for part in aggregated_fix_logs if part).strip()
                if prefix:
                    result["logs"] = (prefix + "\n\n" + logs).strip() if logs else prefix
            result["type"] = "execution"
            result["attempts"] = attempts
            result["fixes"] = applied_fixes
            result["trace_id"] = trace_id
            result["execution_time"] = float(result.get("execution_time") or execution_time)
            result["retry_time"] = retry_time
            result["total_time"] = max(0.0, time.perf_counter() - total_t0)
            return result

        if retries >= MAX_RETRIES:
            if aggregated_fix_logs:
                prefix = "\n\n".join(part for part in aggregated_fix_logs if part).strip()
                if prefix:
                    result["logs"] = (prefix + "\n\n" + logs).strip() if logs else prefix
            await append_run_log(
                server=server,
                workspace_path=workspace_path,
                entry=obs.make_log(
                    level="ERROR",
                    layer="self_healing",
                    message="stop_max_retries",
                    trace_id=trace_id,
                    meta={
                        "job_id": job_id,
                        "attempts": attempts,
                        "retries": retries,
                        "max_retries": MAX_RETRIES,
                        "logs_tail": (str(result.get("logs") or "")[-800:]),
                    },
                ),
                timeout=5,
            )
            result["type"] = "execution"
            result["attempts"] = attempts
            result["fixes"] = applied_fixes
            result["trace_id"] = trace_id
            result["execution_time"] = float(result.get("execution_time") or execution_time)
            result["retry_time"] = retry_time
            result["total_time"] = max(0.0, time.perf_counter() - total_t0)
            return result

        error = parse_error(logs)
        await append_run_log(
            server=server,
            workspace_path=workspace_path,
            entry=obs.make_log(
                level="ERROR",
                layer="self_healing",
                message="error_detected",
                trace_id=trace_id,
                meta={
                    "job_id": job_id,
                    "error_type": error.get("type"),
                    "module": error.get("module"),
                    "message": (error.get("message") or "")[:400],
                },
            ),
            timeout=5,
        )

        fix_applied = False
        next_mode = "DEFAULT"
        fix_t0 = time.perf_counter()

        if error.get("type") == "ModuleNotFoundError" and error.get("module"):
            mod = str(error.get("module") or "").strip()
            pkg = _MODULE_TO_PIP.get(mod, mod)
            fix_key = f"pip_install:{pkg}"
            if fix_key not in used_fix_keys:
                used_fix_keys.add(fix_key)
                install_logs = await _pip_install_missing(
                    server=server,
                    workspace_path=workspace_path,
                    module=mod,
                    timeout=setup_timeout,
                )
                if install_logs:
                    aggregated_fix_logs.append(f"== self_heal: python3 -m pip install {pkg} ==\n{install_logs}")
                applied_fixes.append(f"installed package: {pkg}")
                fix_applied = True

        elif error.get("type") == "ImportError":
            fix_key = "telegram_filters_v20"
            updated = _fix_telegram_filters_import(current_code)
            if updated != current_code and fix_key not in used_fix_keys:
                used_fix_keys.add(fix_key)
                await write_workspace_file(
                    server=server,
                    workspace_path=workspace_path,
                    path=entrypoint,
                    content=updated,
                    allow_write=True,
                    timeout=max(10, int(setup_timeout)),
                )
                current_code = updated
                applied_fixes.append("fixed import: telegram Filters -> filters")
                fix_applied = True

        elif error.get("type") == "SyntaxError":
            if "```" in current_code:
                fix_key = "cleaned_markdown_fences"
                cleaned = _clean_markdown_fences(current_code)
                if cleaned and cleaned != current_code and fix_key not in used_fix_keys:
                    used_fix_keys.add(fix_key)
                    await write_workspace_file(
                        server=server,
                        workspace_path=workspace_path,
                        path=entrypoint,
                        content=cleaned,
                        allow_write=True,
                        timeout=max(10, int(setup_timeout)),
                    )
                    current_code = cleaned
                    applied_fixes.append("cleaned markdown")
                    fix_applied = True
            if not fix_applied:
                fix_key = "minimal_syntax_cleanup"
                cleaned2 = _minimal_syntax_cleanup(current_code, logs)
                if cleaned2 != current_code and fix_key not in used_fix_keys:
                    used_fix_keys.add(fix_key)
                    await write_workspace_file(
                        server=server,
                        workspace_path=workspace_path,
                        path=entrypoint,
                        content=cleaned2,
                        allow_write=True,
                        timeout=max(10, int(setup_timeout)),
                    )
                    current_code = cleaned2
                    applied_fixes.append("applied minimal syntax cleanup")
                    fix_applied = True

        elif error.get("type") == "TimeoutError":
            fix_key = "timeout_to_background"
            if fix_key not in used_fix_keys:
                used_fix_keys.add(fix_key)
                applied_fixes.append("marked long_running; rerun in background")
                next_mode = "BACKGROUND"
                fix_applied = True

        retry_time += max(0.0, time.perf_counter() - fix_t0)

        if not fix_applied:
            if aggregated_fix_logs:
                prefix = "\n\n".join(part for part in aggregated_fix_logs if part).strip()
                if prefix:
                    result["logs"] = (prefix + "\n\n" + logs).strip() if logs else prefix
            await append_run_log(
                server=server,
                workspace_path=workspace_path,
                entry=obs.make_log(
                    level="ERROR",
                    layer="self_healing",
                    message="stop_no_known_fix",
                    trace_id=trace_id,
                    meta={
                        "job_id": job_id,
                        "attempts": attempts,
                        "retries": retries,
                        "error_type": error.get("type"),
                        "module": error.get("module"),
                        "logs_tail": (str(result.get("logs") or "")[-800:]),
                    },
                ),
                timeout=5,
            )
            result["type"] = "execution"
            result["attempts"] = attempts
            result["fixes"] = applied_fixes
            result["trace_id"] = trace_id
            result["execution_time"] = float(result.get("execution_time") or execution_time)
            result["retry_time"] = retry_time
            result["total_time"] = max(0.0, time.perf_counter() - total_t0)
            return result

        retries += 1
        await append_run_log(
            server=server,
            workspace_path=workspace_path,
            entry=obs.make_log(
                level="INFO",
                layer="self_healing",
                message="fix_applied",
                trace_id=trace_id,
                meta={"job_id": job_id, "retry": retries, "max_retries": MAX_RETRIES, "fixes": applied_fixes[-5:]},
            ),
            timeout=5,
        )
