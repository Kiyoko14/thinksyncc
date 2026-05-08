"""Unified ThinkSync agent runtime.

Single source of truth:
- all execution flows through agent_llm.run_tool_calling_loop()
- jobs persist to Supabase
- chat memory persists to Redis + DB
- job event streaming persists to Redis and replays over WebSocket
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sys
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from core.mode_context import reset_request_mode, set_request_mode
from core.value_coercion import value_to_str
from models.agent import AgentDecision, AgentStep, AgentTier, StepResult
from models.job import JobAccepted, JobCreate, JobResponse, JobStatus
from services import agent_llm
from services import logger as obs
from services.chat_service import ChatService
from services.capability_service import detect_capabilities
from services.context_engine import ContextEngine
from services.redis_service import RedisService
from services.memory import MemoryStore
from services.planner import build_plan
from services.executor import run_server_execution
from services.server_service import ServerService
from services import self_healing
from services.ssh_service import SSHService
from services.workspace_service import WorkspaceService
from services.deploy_service import DeployService
from services.guardrails import validate_code, validate_python_syntax, validate_workspace_path
from services.templates import extract_template_params, match_template, render_template
from services.tools import (
    append_run_log,
    exec_in_workspace,
    file_exists_in_workspace,
    write_workspace_file,
)

logger = logging.getLogger(__name__)

_TABLE = "jobs"
_local_subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
_local_event_history: dict[str, list[dict[str, Any]]] = {}
_local_event_seq: dict[str, int] = {}
_semaphore: asyncio.Semaphore | None = None

_GENERIC_RETRY_MESSAGE = "Something went wrong. Retrying..."
_GENERIC_FAILURE_MESSAGE = "No error details were captured from the agent pipeline."


def _strip_markdown_fences(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    lines: list[str] = []
    for line in raw.splitlines():
        if line.strip().startswith("```"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _looks_like_raw_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        token in lowered
        for token in (
            "traceback (most recent call last)",
            "modulenotfounderror",
            "importerror",
            "syntaxerror",
            "indentationerror",
            "exception:",
            "error:",
            "httpsexception",
        )
    )


def _deployment_verified_from_steps(steps: list[Any]) -> bool:
    """Return True only if at least one executed step contains evidence of a
    successful HTTP response — i.e. an actual running HTTP server was confirmed.

    Accepted evidence (case-insensitive):
      • stdout contains an HTTP 200 status token ("200 ok", "http/1", "http/2")
      • stdout contains a curl/wget success line ("< http", "200 ok")
      • step args contain a curl/wget command that returned exit_code 0

    Without this, "All set. Open: …" must never be emitted regardless of whether
    the tool steps individually succeeded.
    """
    # Compile once — only stdout content is trusted; exit codes alone are not
    # sufficient evidence (curl can exit 0 even for failed/redirected requests).
    _HTTP_OK_RE = re.compile(
        r"""
        (?:
            \b200\s+ok\b                    # "200 OK"
            | http/[12][.\d]*\s+200\b       # "HTTP/1.1 200" or "HTTP/2 200"
            | <\s*http/[12]                 # curl verbose header "< HTTP/1.1"
        )
        """,
        re.VERBOSE | re.IGNORECASE,
    )
    for step in steps:
        stdout = str(
            getattr(step, "stdout", "")
            or (step.get("stdout") if isinstance(step, dict) else "")
            or ""
        )
        if _HTTP_OK_RE.search(stdout):
            return True

    return False


def _clean_user_summary(text: str, *, fallback: str) -> str:
    cleaned = _strip_markdown_fences(text)
    cleaned = cleaned.replace("\u0000", "").strip()
    if not cleaned:
        return fallback
    if _looks_like_raw_error(cleaned):
        return fallback
    # Keep it short and human-friendly.
    return cleaned[:1500].strip()


def _extract_message_from_json_summary(summary: str) -> str | None:
    raw = (summary or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    # Legacy shapes: {"type": "...", "content": "..."} or {"type": "...", "message": "..."}
    for key in ("message", "content", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _stringify_error_detail(detail: Any) -> str:
    if isinstance(detail, str):
        return detail.strip()
    if isinstance(detail, dict):
        for key in ("message", "error", "detail", "code"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        try:
            return json.dumps(detail, ensure_ascii=False)
        except Exception:
            return str(detail)
    if isinstance(detail, list):
        parts = [_stringify_error_detail(item) for item in detail]
        joined = "\n".join(part for part in parts if part)
        return joined.strip()
    return str(detail).strip()


def _exception_to_error_string(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = _stringify_error_detail(exc.detail)
        return detail or f"HTTPException: status={exc.status_code}"
    return f"{type(exc).__name__}: {exc}".strip()


def _result_to_error_string(result: Any) -> str:
    if not isinstance(result, dict):
        return _GENERIC_FAILURE_MESSAGE
    for key in ("error", "message"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        joined = "\n".join(_stringify_error_detail(item) for item in errors if _stringify_error_detail(item))
        if joined.strip():
            return joined.strip()
    logs = str(result.get("logs") or "").strip()
    if logs:
        return logs[-4000:]
    summary = str(result.get("summary") or "").strip()
    if summary:
        return summary
    return _GENERIC_FAILURE_MESSAGE


def _redact_step_outputs(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for row in (steps or []):
        if not isinstance(row, dict):
            continue
        clone = dict(row)
        if "stdout" in clone:
            clone["stdout"] = ""
        if "stderr" in clone:
            clone["stderr"] = ""
        redacted.append(clone)
    return redacted


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_agent_response(*, response_type: str, content: str) -> str:
    return json.dumps({"type": response_type, "content": content or ""}, ensure_ascii=False)


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().AGENT_MAX_CONCURRENCY)
    return _semaphore


def _db_update(job_id: str, patch: dict[str, Any]) -> None:
    try:
        patch["updated_at"] = _now_iso()
        get_supabase().table(_TABLE).update(patch).eq("id", job_id).execute()
    except APIError as exc:
        # Backward compatibility if migrations haven't been applied yet.
        msg = str(exc)
        if any(token in msg for token in ("intent", "errors", "retries")) and ("column" in msg or "field" in msg):
            fallback = dict(patch)
            fallback.pop("intent", None)
            fallback.pop("errors", None)
            fallback.pop("retries", None)
            try:
                get_supabase().table(_TABLE).update(fallback).eq("id", job_id).execute()
                return
            except APIError as exc2:
                logger.warning("jobs UPDATE failed (fallback, job=%s): %s", job_id, exc2)
        logger.warning("jobs UPDATE failed (job=%s): %s", job_id, exc)


_memory_store = MemoryStore()

_SERVER_APP_RE = re.compile(r"(?is)\b(flask|fastapi|uvicorn)\b")
_FLASK_RE = re.compile(r"(?is)\bfrom\s+flask\s+import\b|\bFlask\s*\(")
_FASTAPI_RE = re.compile(r"(?is)\bfrom\s+fastapi\s+import\b|\bFastAPI\s*\(")
_PORT_RE = re.compile(r"(?is)\bport\s*=\s*(\d{2,5})\b")
_LONG_RUNNING_RE = re.compile(r"(?is)\b(run_polling\s*\(|app\.run\s*\(|uvicorn\b|flask\s+run\b)")


def _workspace_name_from_objective(objective: str) -> str:
    cleaned = (objective or "").strip()
    if not cleaned:
        return "workspace"
    # Use first line / sentence as a human-friendly name.
    first = cleaned.splitlines()[0].strip()
    first = re.sub(r"[\[\]\(\)\{\}<>:\"'`]", " ", first)
    first = re.sub(r"\s{2,}", " ", first).strip()
    return (first[:60] or "workspace")


def _sanitize_workspace_name(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return "workspace"
    cleaned: list[str] = []
    prev_dash = False
    for ch in raw:
        is_alnum = ("a" <= ch <= "z") or ("0" <= ch <= "9")
        if is_alnum:
            cleaned.append(ch)
            prev_dash = False
            continue
        if ch.isspace() or ch in {"-", "_"}:
            if not prev_dash:
                cleaned.append("-")
                prev_dash = True
            continue
    name = "".join(cleaned).strip("-")
    name = "-".join(part for part in name.split("-") if part)
    return (name[:60] or "workspace")


def _detect_server_app(code: str) -> tuple[bool, int]:
    text = code or ""
    if not _SERVER_APP_RE.search(text):
        return (False, 0)
    port = 0
    match = _PORT_RE.search(text)
    if match:
        try:
            port = int(match.group(1))
        except Exception:
            port = 0
    if port <= 0:
        if _FLASK_RE.search(text):
            port = 5000
        elif _FASTAPI_RE.search(text):
            port = 8000
        else:
            port = 8000
    return (True, port)


def _detect_long_running_app(code: str) -> bool:
    return bool(_LONG_RUNNING_RE.search(code or ""))


def _trim_logs(value: str, limit: int = 12000) -> str:
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...<trimmed>\n"


def clean_code_output(text: str) -> str:
    text = (text or "").strip()

    pattern = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
    matches = pattern.findall(text)

    if matches:
        code = "\n\n".join(m.strip() for m in matches if m.strip())
    else:
        code = text

    return normalize_python_booleans(code.strip().strip("`").strip())


def normalize_python_booleans(code: str) -> str:
    # Common LLM bug: JSON booleans in Python code (NameError: true/false).
    text = code or ""
    text = re.sub(r"\btrue\b", "True", text)
    text = re.sub(r"\bfalse\b", "False", text)
    return text


_MODULE_TO_PIP: dict[str, str] = {
    "telegram": "python-telegram-bot",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
}


def _stdlib_modules() -> frozenset[str]:
    names = getattr(sys, "stdlib_module_names", None)
    if isinstance(names, (set, frozenset)):
        return frozenset(names)
    # Fallback: common stdlib/builtins (non-exhaustive; prefer allow-install over blocking execution)
    return frozenset(
        {
            "abc",
            "argparse",
            "asyncio",
            "base64",
            "collections",
            "contextlib",
            "copy",
            "csv",
            "dataclasses",
            "datetime",
            "decimal",
            "enum",
            "functools",
            "hashlib",
            "http",
            "io",
            "itertools",
            "json",
            "logging",
            "math",
            "os",
            "pathlib",
            "queue",
            "random",
            "re",
            "shlex",
            "signal",
            "socket",
            "sqlite3",
            "ssl",
            "statistics",
            "string",
            "subprocess",
            "sys",
            "tempfile",
            "textwrap",
            "threading",
            "time",
            "traceback",
            "typing",
            "types",
            "uuid",
            "xml",
        }
    )


def _is_stdlib_module(name: str) -> bool:
    cleaned = (name or "").strip()
    if not cleaned:
        return True
    if cleaned in sys.builtin_module_names:
        return True
    if cleaned in _stdlib_modules():
        return True
    # Some stdlib modules live under package namespaces (e.g. http.client)
    top = cleaned.split(".", 1)[0]
    return top in _stdlib_modules() or top in sys.builtin_module_names


def _extract_import_modules(code: str) -> list[str]:
    modules: set[str] = set()
    text = code or ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m1 = re.match(r"^\s*import\s+(.+)$", line)
        if m1:
            chunk = m1.group(1)
            # "a, b as c"
            for part in chunk.split(","):
                part = part.strip()
                if not part:
                    continue
                name = part.split(" as ", 1)[0].strip()
                if not name:
                    continue
                top = name.split(".", 1)[0].strip()
                if top:
                    modules.add(top)
            continue

        m2 = re.match(r"^\s*from\s+([a-zA-Z0-9_.]+)\s+import\b", line)
        if m2:
            name = m2.group(1).strip()
            if not name or name.startswith("."):
                continue
            top = name.split(".", 1)[0].strip()
            if top:
                modules.add(top)
            continue

    # Avoid installing local project modules: only consider names that look like third-party candidates.
    return sorted(modules)


def _modules_to_packages(modules: list[str]) -> list[str]:
    pkgs: set[str] = set()
    for mod in modules:
        if _is_stdlib_module(mod):
            continue
        pkgs.add(_MODULE_TO_PIP.get(mod, mod))
    return sorted(pkgs)


def _extract_missing_module_from_error(text: str) -> str | None:
    combined = text or ""
    match = re.search(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", combined)
    if not match:
        return None
    name = (match.group(1) or "").strip()
    if not name:
        return None
    return name.split(".", 1)[0].strip() or None


async def _ensure_pip_available(
    *,
    server: dict[str, Any],
    workspace_path: str,
    timeout: int,
    job_id: str,
    logs_parts: list[str],
) -> bool:
    v_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m pip --version",
        timeout=timeout,
    )
    if v_res["stdout"] or v_res["stderr"]:
        logs_parts.append("== pip --version ==\n" + (v_res["stdout"] + v_res["stderr"]))
    if v_res["code"] == 0:
        logger.info("[pip] available | job=%s", job_id)
        return True

    logger.info("[pip] missing | attempting ensurepip | job=%s", job_id)
    e_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m ensurepip --upgrade",
        timeout=timeout,
    )
    if e_res["stdout"] or e_res["stderr"]:
        logs_parts.append("== ensurepip --upgrade ==\n" + (e_res["stdout"] + e_res["stderr"]))

    v2_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m pip --version",
        timeout=timeout,
    )
    if v2_res["stdout"] or v2_res["stderr"]:
        logs_parts.append("== pip --version (after ensurepip) ==\n" + (v2_res["stdout"] + v2_res["stderr"]))
    if v2_res["code"] == 0:
        logger.info("[pip] installed via ensurepip | job=%s", job_id)
        return True

    logger.info("[pip] ensurepip failed | attempting apt install python3-pip | job=%s", job_id)
    a_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="apt update",
        timeout=timeout,
    )
    if a_res["stdout"] or a_res["stderr"]:
        logs_parts.append("== apt update ==\n" + (a_res["stdout"] + a_res["stderr"]))

    i_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="apt install -y python3-pip",
        timeout=timeout,
    )
    if i_res["stdout"] or i_res["stderr"]:
        logs_parts.append("== apt install -y python3-pip ==\n" + (i_res["stdout"] + i_res["stderr"]))

    v3_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m pip --version",
        timeout=timeout,
    )
    if v3_res["stdout"] or v3_res["stderr"]:
        logs_parts.append("== pip --version (after apt) ==\n" + (v3_res["stdout"] + v3_res["stderr"]))
    if v3_res["code"] == 0:
        logger.info("[pip] installed via apt | job=%s", job_id)
        return True

    logger.warning("[pip] unavailable after attempts | job=%s", job_id)
    return False


async def _run_code_execution(
    *,
    job_id: str,
    payload: JobCreate,
    user_id: str,
    server: dict[str, Any],
    conversation_history: list[dict[str, str]],
    step_timeout: int,
    trace_id: str,
) -> dict[str, Any]:
    overall_t0 = time.perf_counter()
    allow_write = True
    payload.allow_write = True
    logger.info("Execution forced: allow_write=True")
    logger.info("[code] allow_write=%s | job=%s", allow_write, job_id)

    # STEP 2: Resolve workspace
    workspace: dict[str, Any] | None = None
    workspace_id = payload.workspace_id
    if workspace_id:
        try:
            workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)
        except Exception:
            workspace = None
    else:
        workspace_name = _workspace_name_from_objective(payload.objective)
        try:
            workspace = await WorkspaceService.resolve_workspace(user_id=user_id, server_id=payload.server_id, name=workspace_name)
        except Exception:
            workspace = None

    if workspace is None:
        workspace = await WorkspaceService.create_workspace_from_prompt(user_id=user_id, server_id=payload.server_id, user_input=payload.objective)

    workspace_id = str(workspace.get("id") or "")
    payload.workspace_id = workspace_id or None
    _db_update(job_id, {"workspace_id": payload.workspace_id})

    if workspace.get("server_id") != payload.server_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="workspace_id does not belong to the provided server_id")

    workspace_name = str(workspace.get("slug") or "").strip().lower() or _sanitize_workspace_name(payload.objective)
    workspace_path = f"/root/workspaces/{workspace_name}"
    if not workspace_path.startswith("/root/workspaces"):
        workspace_path = f"/root/workspaces/{_sanitize_workspace_name(payload.objective)}"
    os.makedirs(workspace_path, exist_ok=True)
    await SSHService.execute(server=server, command=f"mkdir -p {shlex.quote(workspace_path)}", command_timeout=step_timeout)
    logger.info("[workspace] name=%s | path=%s | job=%s", workspace_name, workspace_path, job_id)
    logger.info(
        "[code] workspace_resolved | job=%s | workspace_id=%s | slug=%s | path=%s",
        job_id,
        workspace_id,
        str(workspace.get("slug") or ""),
        workspace_path,
    )
    await append_run_log(
        server=server,
        workspace_path=workspace_path,
        entry=obs.make_log(
            level="INFO",
            layer="router",
            message="workspace_resolved",
            trace_id=trace_id,
            meta={"job_id": job_id, "workspace": workspace_name, "mode": "agent"},
        ),
        timeout=5,
    )

    # Ensure chat exists and persist user message now that workspace is known.
    ChatService.create_chat(workspace_id=workspace_id, user_id=user_id)
    ChatService.save_workspace_message(workspace_id=workspace_id, user_id=user_id, role="user", content=payload.objective)

    ws_validation = validate_workspace_path(workspace_path)
    if not ws_validation.get("valid"):
        logger.warning("[guardrails] blocked_workspace | job=%s | errors=%s", job_id, ws_validation.get("errors"))
        return {"type": "validation_error", "errors": ws_validation.get("errors") or ["invalid workspace path"]}

    # Deterministic template/tool layer (pre-LLM).
    template = match_template(payload.objective or "")
    if template is not None:
        logger.info("[template] matched | name=%s | job=%s", template.name, job_id)
        await append_run_log(
            server=server,
            workspace_path=workspace_path,
            entry=obs.make_log(
                level="INFO",
                layer="template",
                message="template_matched",
                trace_id=trace_id,
                meta={"job_id": job_id, "template": template.name, "mode": "template"},
            ),
            timeout=5,
        )
        rendered = render_template(template, extract_template_params(payload.objective or ""))
        rendered_files = rendered.get("files") or {}

        logs_parts: list[str] = []
        main_code = normalize_python_booleans(str(rendered_files.get("main.py") or ""))
        validation = validate_code(main_code)
        logger.info(
            "[guardrails] validate | source=template | job=%s | valid=%s | errors=%s | warnings=%s",
            job_id,
            bool(validation.get("valid")),
            validation.get("errors") or [],
            validation.get("warnings") or [],
        )
        await append_run_log(
            server=server,
            workspace_path=workspace_path,
            entry=obs.make_log(
                level="INFO" if validation.get("valid") else "ERROR",
                layer="guardrails",
                message="validate_code",
                trace_id=trace_id,
                meta={
                    "job_id": job_id,
                    "source": "template",
                    "valid": bool(validation.get("valid")),
                    "errors": validation.get("errors") or [],
                    "warnings": validation.get("warnings") or [],
                    "mode": "template",
                },
            ),
            timeout=5,
        )
        if not validation.get("valid"):
            return {"type": "validation_error", "errors": validation.get("errors") or ["validation failed"]}
        main_code = str(validation.get("sanitized_code") or main_code)
        rendered_files["main.py"] = main_code

        for path, content in rendered_files.items():
            w_res = await write_workspace_file(
                server=server,
                workspace_path=workspace_path,
                path=str(path),
                content=str(content or ""),
                allow_write=allow_write,
                timeout=step_timeout,
            )
            if w_res["stdout"] or w_res["stderr"]:
                logs_parts.append(f"== write {path} ==\n" + (w_res['stdout'] or w_res['stderr']))
            if w_res["code"] != 0:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"code": "WRITE_FAILED", "path": path, "logs": (w_res["stderr"] or w_res["stdout"])},
                )

        syntax = await validate_python_syntax(server=server, workspace_path=workspace_path, entrypoint="main.py", timeout=15)
        logger.info(
            "[guardrails] py_compile | source=template | job=%s | valid=%s | errors=%s",
            job_id,
            bool(syntax.get("valid")),
            syntax.get("errors") or [],
        )
        await append_run_log(
            server=server,
            workspace_path=workspace_path,
            entry=obs.make_log(
                level="INFO" if syntax.get("valid") else "ERROR",
                layer="guardrails",
                message="validate_python_syntax",
                trace_id=trace_id,
                meta={
                    "job_id": job_id,
                    "source": "template",
                    "valid": bool(syntax.get("valid")),
                    "errors": syntax.get("errors") or [],
                    "mode": "template",
                },
            ),
            timeout=5,
        )
        if not syntax.get("valid"):
            return {"type": "validation_error", "errors": syntax.get("errors") or ["py_compile failed"]}

        result = await self_healing.execute_with_self_healing(
            server=server,
            workspace_path=workspace_path,
            code=main_code,
            entrypoint="main.py",
            setup_timeout=step_timeout,
            job_id=job_id,
            trace_id=trace_id,
        )

        if isinstance(result, dict) and logs_parts and "logs" in result:
            prefix = _trim_logs("\n".join(part for part in logs_parts if part).strip())
            if prefix:
                result["logs"] = (prefix + "\n\n" + str(result.get("logs") or "")).strip()
        if isinstance(result, dict) and result.get("type") == "background":
            result["workspace"] = workspace_name
            result.pop("logs", None)
        if isinstance(result, dict) and not bool(result.get("success")) and result.get("type") != "background":
            await append_run_log(
                server=server,
                workspace_path=workspace_path,
                entry=obs.make_log(
                    level="ERROR",
                    layer="execution",
                    message="execution_failed",
                    trace_id=trace_id,
                    meta={
                        "job_id": job_id,
                        "error_type": "execution",
                        "attempts": result.get("attempts"),
                        "fixes": result.get("fixes") or [],
                        "logs_tail": (str(result.get("logs") or "")[-800:]),
                        "mode": "template",
                    },
                ),
                timeout=5,
            )
        if isinstance(result, dict):
            result["trace_id"] = trace_id
            result["total_time"] = max(0.0, time.perf_counter() - overall_t0)
        return result

    logger.info("[template] no_match | fallback_llm=true | job=%s", job_id)
    await append_run_log(
        server=server,
        workspace_path=workspace_path,
        entry=obs.make_log(
            level="INFO",
            layer="template",
            message="template_no_match",
            trace_id=trace_id,
            meta={"job_id": job_id, "mode": "llm"},
        ),
        timeout=5,
    )

    context_bundle = await ContextEngine.build_context(
        workspace_id=workspace_id,
        task=payload.objective,
        server=server,
        workspace_path=workspace_path,
    )
    logger.info(
        "[context] mode=%s | selected_files=%s | job=%s",
        context_bundle.get("mode"),
        context_bundle.get("selected_files") or [],
        job_id,
    )

    if str(context_bundle.get("mode") or "").upper() == "PATCH":
        existing_files = [
            {"path": item.get("path"), "content": item.get("content")}
            for item in (context_bundle.get("snippets") or [])
            if isinstance(item, dict) and item.get("path") and item.get("content") is not None
        ]
        if not existing_files:
            return {"type": "validation_error", "errors": ["PATCH mode selected but no real workspace files were loaded"]}

        patch_result = await agent_llm.run_safe_patch_edit(
            existing_files=existing_files,
            task=payload.objective,
            constraints={
                "mode": "PATCH",
                "max_files": get_settings().AGENT_CONTEXT_MAX_FILES,
                "max_total_lines": get_settings().AGENT_CONTEXT_MAX_TOTAL_LINES,
            },
            conversation_history=conversation_history,
            context_bundle=context_bundle,
        )
        if patch_result.get("status") == "processing":
            return {
                "type": "processing",
                "mode": "PATCH",
                "success": False,
                "summary": "processing",
                "trace_id": trace_id,
                "total_time": max(0.0, time.perf_counter() - overall_t0),
            }
        if (patch_result.get("report") or {}).get("status") != "success":
            return {
                "type": "validation_error",
                "errors": [str((patch_result.get("report") or {}).get("summary") or "Patch generation failed")],
                "details": patch_result.get("failure_history") or [],
            }

        updated_files = patch_result.get("updated_files") or []
        before_by_path = {str(item.get("path") or ""): str(item.get("content") or "") for item in existing_files}
        changed_files: list[dict[str, Any]] = []
        for item in updated_files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            after = str(item.get("content") or "")
            before = before_by_path.get(path)
            if not path or before is None or before == after:
                continue
            w_res = await write_workspace_file(
                server=server,
                workspace_path=workspace_path,
                path=path,
                content=after,
                allow_write=allow_write,
                timeout=step_timeout,
            )
            if w_res["code"] != 0:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"code": "WRITE_FAILED", "path": path, "logs": (w_res["stderr"] or w_res["stdout"])},
                )
            changed_files.append({"path": path, "before": before, "after": after})

        if not changed_files:
            return {"type": "validation_error", "errors": ["Patch mode produced no file changes"]}

        return {
            "type": "patch",
            "mode": "PATCH",
            "success": True,
            "files": changed_files,
            "selected_files": context_bundle.get("selected_files") or [],
            "summary": (patch_result.get("report") or {}).get("summary") or f"Patched {len(changed_files)} file(s).",
            "trace_id": trace_id,
            "total_time": max(0.0, time.perf_counter() - overall_t0),
        }

    # STEP 1: Generate code
    code = await agent_llm.generate_code_response(
        payload.objective,
        conversation_history=conversation_history,
        context_bundle=context_bundle,
    )
    if not code.strip():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="LLM returned empty code")
    clean_code = clean_code_output(code)
    logger.info("Code cleaned before execution")
    logger.info("[code] cleaned_preview=%r | job=%s", (clean_code or "")[:600], job_id)

    validation = validate_code(clean_code)
    logger.info(
        "[guardrails] validate | source=llm | job=%s | valid=%s | errors=%s | warnings=%s",
        job_id,
        bool(validation.get("valid")),
        validation.get("errors") or [],
        validation.get("warnings") or [],
    )
    if not validation.get("valid"):
        return {"type": "validation_error", "errors": validation.get("errors") or ["validation failed"]}
    clean_code = normalize_python_booleans(str(validation.get("sanitized_code") or clean_code))
    await append_run_log(
        server=server,
        workspace_path=workspace_path,
        entry=obs.make_log(
            level="INFO" if validation.get("valid") else "ERROR",
            layer="guardrails",
            message="validate_code",
            trace_id=trace_id,
            meta={
                "job_id": job_id,
                "source": "llm",
                "valid": bool(validation.get("valid")),
                "errors": validation.get("errors") or [],
                "warnings": validation.get("warnings") or [],
                "mode": "llm",
            },
        ),
        timeout=5,
    )

    # STEP 3: Write file
    files_written: list[str] = ["main.py"]
    w_res = await write_workspace_file(
        server=server,
        workspace_path=workspace_path,
        path="main.py",
        content=clean_code,
        allow_write=allow_write,
        timeout=step_timeout,
    )
    logs_parts: list[str] = []
    if w_res["stdout"] or w_res["stderr"]:
        logs_parts.append("== write_file ==\n" + (w_res["stdout"] or w_res["stderr"]))
    if w_res["code"] != 0:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"code": "WRITE_FAILED", "logs": (w_res["stderr"] or w_res["stdout"])})

    syntax = await validate_python_syntax(server=server, workspace_path=workspace_path, entrypoint="main.py", timeout=15)
    logger.info(
        "[guardrails] py_compile | source=llm | job=%s | valid=%s | errors=%s",
        job_id,
        bool(syntax.get("valid")),
        syntax.get("errors") or [],
    )
    await append_run_log(
        server=server,
        workspace_path=workspace_path,
        entry=obs.make_log(
            level="INFO" if syntax.get("valid") else "ERROR",
            layer="guardrails",
            message="validate_python_syntax",
            trace_id=trace_id,
            meta={
                "job_id": job_id,
                "source": "llm",
                "valid": bool(syntax.get("valid")),
                "errors": syntax.get("errors") or [],
                "mode": "llm",
            },
        ),
        timeout=5,
    )
    if not syntax.get("valid"):
        return {"type": "validation_error", "errors": syntax.get("errors") or ["py_compile failed"]}

    result = await self_healing.execute_with_self_healing(
        server=server,
        workspace_path=workspace_path,
        code=clean_code,
        entrypoint="main.py",
        setup_timeout=step_timeout,
        job_id=job_id,
        trace_id=trace_id,
    )

    if isinstance(result, dict) and logs_parts and "logs" in result:
        prefix = _trim_logs("\n".join(part for part in logs_parts if part).strip())
        if prefix:
            result["logs"] = (prefix + "\n\n" + str(result.get("logs") or "")).strip()

    # Workspace name should be slug, not absolute path.
    if isinstance(result, dict) and result.get("type") == "background":
        result["workspace"] = workspace_name
    if isinstance(result, dict) and not bool(result.get("success")) and result.get("type") != "background":
        await append_run_log(
            server=server,
            workspace_path=workspace_path,
            entry=obs.make_log(
                level="ERROR",
                layer="execution",
                message="execution_failed",
                trace_id=trace_id,
                meta={
                    "job_id": job_id,
                    "error_type": "execution",
                    "attempts": result.get("attempts"),
                    "fixes": result.get("fixes") or [],
                    "logs_tail": (str(result.get("logs") or "")[-800:]),
                    "mode": "llm",
                },
            ),
            timeout=5,
        )
    if isinstance(result, dict):
        result["trace_id"] = trace_id
        result["total_time"] = max(0.0, time.perf_counter() - overall_t0)

    return result


def _step_to_record(result: StepResult) -> dict[str, Any]:
    return {
        "step": result.step,
        "tool": value_to_str(getattr(result, "tool", None)),
        "args": result.args,
        "command": result.command,
        "command_type": result.command_type,
        "stdout": result.stdout[:4000],
        "stderr": result.stderr[:2000],
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "success": result.success,
        "validation_passed": result.validation_passed,
        "status": result.status,
        "agent_reasoning": result.agent_reasoning,
        "executed_at": result.executed_at.isoformat(),
    }


def _decision_to_record(decision: AgentDecision) -> dict[str, Any]:
    return {
        "action": value_to_str(getattr(decision, "action", None)),
        "reason": decision.reason,
        "summary_so_far": decision.summary_so_far,
        "modified_step": decision.modified_step.model_dump(mode="json") if decision.modified_step else None,
    }


def _row_to_response(row: dict[str, Any]) -> JobResponse:
    return JobResponse(
        id=row["id"],
        user_id=row["user_id"],
        workspace_id=row.get("workspace_id"),
        server_id=row["server_id"],
        objective=row["objective"],
        status=JobStatus(row["status"]),
        allow_write=bool(row.get("allow_write", False)),
        dry_run=bool(row.get("dry_run", False)),
        intent=row.get("intent") or "chat",
        task_mode=value_to_str(row.get("task_mode") or "complex"),
        plan=row.get("plan") or [],
        steps=_redact_step_outputs(row.get("steps") or []),
        decisions=[],
        errors=[],
        retries=[],
        summary=_extract_message_from_json_summary(str(row.get("summary") or "")) or str(row.get("summary") or ""),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _next_event_sequence(job_id: str) -> int:
    redis = RedisService.get_async_client()
    if redis is not None:
        try:
            return int(await redis.incr(f"job_events:{job_id}:seq"))
        except Exception as exc:
            logger.warning("Redis INCR failed for job=%s: %s", job_id, exc)
    next_value = _local_event_seq.get(job_id, 0) + 1
    _local_event_seq[job_id] = next_value
    return next_value


async def _publish(job_id: str, event: dict[str, Any]) -> None:
    settings = get_settings()
    enriched = {
        "timestamp": event.get("timestamp") or _now_iso(),
        "sequence": event.get("sequence") or await _next_event_sequence(job_id),
        **event,
    }
    encoded = json.dumps(enriched)
    redis = RedisService.get_async_client()
    if redis is not None:
        history_key = f"job_events:{job_id}"
        channel = f"job_events:{job_id}:live"
        try:
            await redis.rpush(history_key, encoded)
            await redis.ltrim(history_key, -settings.REDIS_JOB_EVENT_MAX_ITEMS, -1)
            if settings.REDIS_JOB_EVENT_TTL_SECONDS > 0:
                await redis.expire(history_key, settings.REDIS_JOB_EVENT_TTL_SECONDS)
                await redis.expire(f"job_events:{job_id}:seq", settings.REDIS_JOB_EVENT_TTL_SECONDS)
            await redis.publish(channel, encoded)
        except Exception as exc:
            logger.warning("Redis publish failed for job=%s: %s", job_id, exc)

    history = _local_event_history.setdefault(job_id, [])
    history.append(enriched)
    history[:] = history[-settings.REDIS_JOB_EVENT_MAX_ITEMS:]
    for queue in list(_local_subscribers.get(job_id, set())):
        try:
            queue.put_nowait(enriched)
        except asyncio.QueueFull:
            pass


async def run_agent_pipeline(*, job_id: str, payload: JobCreate, user_id: str, trace_id: str | None = None) -> None:
    trace_id = (trace_id or "").strip() or obs.new_trace_id()
    settings = get_settings()
    step_timeout = payload.step_timeout_seconds or settings.AGENT_STEP_TIMEOUT

    _db_update(job_id, {"status": JobStatus.RUNNING.value})
    obs.emit(level="INFO", layer="router", message="job_start", trace_id=trace_id, meta={"job_id": job_id, "mode": "job"})
    await _publish(job_id, {"type": "status_update", "status": JobStatus.RUNNING.value, "step": 0, "tool": None, "trace_id": trace_id})

    requested_mode = (value_to_str(getattr(payload, "mode", None)) or "").strip().lower()

    # Plan mode is chat-only. It must not execute code, touch servers, or write files.
    if requested_mode == "plan":
        try:
            response = await agent_llm.generate_chat_response(user_input=payload.objective, conversation_history=[])
            message = _clean_user_summary(response, fallback=_GENERIC_FAILURE_MESSAGE)
        except Exception as exc:
            obs.emit(
                level="ERROR",
                layer="router",
                message="plan_mode_chat_failed",
                trace_id=trace_id,
                meta={"job_id": job_id, "error_type": type(exc).__name__, "detail": str(exc)},
                exc_info=True,
            )
            message = _exception_to_error_string(exc)

        _db_update(
            job_id,
            {
                "status": JobStatus.COMPLETED.value,
                "summary": message,
                "intent": "chat",
                "task_mode": "simple",
                "plan": [],
                "steps": [],
                "decisions": [],
                "errors": [],
                "retries": [],
            },
        )
        await _publish(job_id, {"type": "completed", "success": True, "summary": message, "step": 0, "tool": None, "trace_id": trace_id})
        return

    allow_write = True
    payload.allow_write = True
    logger.info("Execution forced: allow_write=True")

    try:
        server = ServerService.get_server(server_id=payload.server_id, user_id=user_id)
    except HTTPException as exc:
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": exc.detail})
        obs.emit(
            level="ERROR",
            layer="router",
            message="job_server_lookup_failed",
            trace_id=trace_id,
            meta={"job_id": job_id, "error_type": "HTTPException", "detail": str(exc.detail)},
            exc_info=False,
        )
        await _publish(job_id, {"type": "completed", "success": False, "summary": str(exc.detail), "step": 0, "tool": None, "trace_id": trace_id})
        return

    # WORKSPACE DEFAULT: resolve/create workspace when missing.
    if not payload.workspace_id:
        try:
            workspace_name = _workspace_name_from_objective(payload.objective)
            workspace = await WorkspaceService.resolve_workspace(user_id=user_id, server_id=payload.server_id, name=workspace_name)
            payload.workspace_id = str(workspace.get("id") or "") or None
            _db_update(job_id, {"workspace_id": payload.workspace_id})
        except Exception as exc:
            detail = f"Failed to resolve workspace: {exc}"
            _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": detail})
            obs.emit(
                level="ERROR",
                layer="router",
                message="workspace_resolve_failed",
                trace_id=trace_id,
                meta={"job_id": job_id, "error_type": type(exc).__name__, "detail": str(exc)},
            )
            await _publish(job_id, {"type": "completed", "success": False, "summary": detail, "step": 0, "tool": None, "trace_id": trace_id})
            return

    logger.info("[router] job=%s | allow_write=%s | workspace_id=%s", job_id, allow_write, payload.workspace_id)

    # Conversation context is only available when a workspace exists.
    conversation_history: list[dict[str, str]] = []
    if payload.workspace_id:
        try:
            conversation_history = ChatService.get_recent_context_messages(
                workspace_id=payload.workspace_id,
                user_id=user_id,
                limit=20,
                current_input=payload.objective,
            )
        except Exception:
            conversation_history = []

    accumulated_steps: list[dict[str, Any]] = []
    accumulated_decisions: list[dict[str, Any]] = []

    memory: list[dict[str, Any]] = await _memory_store.load(user_id=user_id, workspace_id=payload.workspace_id)

    # ---------------------------------------------------------------------
    # Intent + task-mode routing (CRITICAL)
    # - Tools may run ONLY when intent == "server"
    # - Complex tasks must produce a plan
    # ---------------------------------------------------------------------
    intent = (await agent_llm.classify_intent(user_input=payload.objective, conversation_history=conversation_history)).strip().lower()
    if intent not in {"chat", "code", "server"}:
        intent = "code"
    deployment_intent = bool(re.search(r"\b(deploy|server|app|run|website)\b", payload.objective or "", re.IGNORECASE))
    if deployment_intent:
        intent = "server"
    if intent == "chat":
        intent = "code"
    if intent == "server" and not deployment_intent:
        lowered_obj = (payload.objective or "").lower()
        if re.search(r"\b(telegram|bot|yoz|kod|code|python|script|program|programma)\b", lowered_obj):
            intent = "code"
    logger.info("[router] input=%r | detected_intent=%s", (payload.objective or "")[:500], intent)
    _db_update(job_id, {"intent": intent, "status": JobStatus.WAITING_FOR_LLM.value})
    await _publish(
        job_id,
        {
            "type": "status_update",
            "status": JobStatus.WAITING_FOR_LLM.value,
            "step": 0,
            "tool": "intent_classifier",
            "intent": intent,
            "trace_id": trace_id,
        },
    )

    task_mode = await agent_llm.detect_task_mode(
        intent=intent,
        user_input=payload.objective,
        conversation_history=conversation_history,
    )
    task_mode = (value_to_str(task_mode) or "complex").strip().lower()
    if task_mode not in {"simple", "complex"}:
        task_mode = "complex"
    _db_update(job_id, {"task_mode": task_mode, "status": JobStatus.WAITING_FOR_LLM.value})
    await _publish(
        job_id,
        {
            "type": "status_update",
            "status": JobStatus.WAITING_FOR_LLM.value,
            "step": 0,
            "tool": "task_mode_detector",
            "task_mode": value_to_str(task_mode),
            "trace_id": trace_id,
        },
    )

    if intent == "code":
        _db_update(job_id, {"status": JobStatus.WAITING_FOR_LLM.value, "task_mode": "simple", "plan": []})
        await _publish(job_id, {"type": "status_update", "status": JobStatus.WAITING_FOR_LLM.value, "step": 0, "tool": "code_executor", "trace_id": trace_id})
        try:
            logger.info("[agent] before_code_execution | job=%s | objective=%r | workspace_id=%s | server_id=%s", job_id, payload.objective, payload.workspace_id, payload.server_id)
            result = await _run_code_execution(
                job_id=job_id,
                payload=payload,
                user_id=user_id,
                server=server,
                conversation_history=conversation_history,
                step_timeout=step_timeout,
                trace_id=trace_id,
            )
            logger.info("[agent] code_execution_result | job=%s | result=%s", job_id, result)
        except HTTPException as exc:
            obs.emit(
                level="ERROR",
                layer="execution",
                message="execution_failed_http",
                trace_id=trace_id,
                meta={"job_id": job_id, "error_type": "HTTPException", "detail": str(exc.detail)},
            )
            logger.exception("[agent] code_execution_http_exception | job=%s", job_id, exc_info=exc)
            summary = _exception_to_error_string(exc)
            _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": summary})
            await _publish(job_id, {"type": "completed", "success": False, "summary": summary, "step": 0, "tool": None, "trace_id": trace_id})
            return
        except Exception as exc:
            obs.emit(
                level="ERROR",
                layer="execution",
                message="execution_failed_exception",
                trace_id=trace_id,
                meta={"job_id": job_id, "error_type": type(exc).__name__, "detail": str(exc)},
                exc_info=True,
            )
            logger.exception("[agent] code_execution_exception | job=%s", job_id)
            summary = _exception_to_error_string(exc)
            _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": summary})
            await _publish(job_id, {"type": "completed", "success": False, "summary": summary, "step": 0, "tool": None, "trace_id": trace_id})
            return

        try:
            success = bool(result.get("success")) if isinstance(result, dict) else False
            exec_time = float(result.get("execution_time") or 0.0) if isinstance(result, dict) else 0.0
        except Exception:
            success = False
            exec_time = 0.0
        obs.METRICS.record_request(success=success, execution_time_seconds=exec_time)
        success = bool(result.get("success")) if isinstance(result, dict) else False
        if isinstance(result, dict) and result.get("type") == "background":
            success = True
        final_status = JobStatus.COMPLETED.value if success else JobStatus.FAILED.value
        workspace_url: str | None = None
        if payload.workspace_id:
            try:
                ws = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=user_id)
                workspace_url = str(ws.get("url") or "") or (f"https://{ws.get('domain')}" if ws.get("domain") else None)
            except Exception:
                workspace_url = None

        if success:
            if isinstance(result, dict) and result.get("type") == "patch":
                summary = json.dumps({"files": result.get("files") or []}, ensure_ascii=False)
            else:
                # BUG #2 guard — only emit URL after verified HTTP evidence in steps.
                # workspace_url existing in DB does not mean the deployment is live.
                steps_evidence = result.get("steps") or [] if isinstance(result, dict) else []
                if workspace_url and _deployment_verified_from_steps(steps_evidence):
                    summary = f"All set. Open: {workspace_url}"
                    logger.info("[agent] deployment_url_verified | url=%s | job=%s", workspace_url, job_id)
                else:
                    summary = _clean_user_summary(
                        str(result.get("summary") or result.get("message") or "") if isinstance(result, dict) else "",
                        fallback="All set.",
                    )
        else:
            summary = _result_to_error_string(result)

        _db_update(job_id, {"status": final_status, "summary": summary})
        if payload.workspace_id:
            try:
                ChatService.save_workspace_message(workspace_id=payload.workspace_id, user_id=user_id, role="assistant", content=summary)
            except Exception:
                logger.warning("Failed to persist assistant message for job=%s", job_id)
        await _publish(job_id, {"type": "completed", "status": final_status, "success": success, "summary": summary, "step": 0, "tool": None})
        return

    if intent == "server" and payload.workspace_id:
        try:
            ChatService.create_chat(workspace_id=payload.workspace_id, user_id=user_id)
            ChatService.save_workspace_message(workspace_id=payload.workspace_id, user_id=user_id, role="user", content=payload.objective)
        except Exception:
            pass

    plan_bundle = await build_plan(
        intent=intent,
        task_mode=task_mode,
        objective=payload.objective,
        max_steps=payload.max_steps,
        allow_write=allow_write,
        server=server if intent == "server" else None,
        conversation_history=conversation_history,
        memory=memory,
    )
    planned_task_mode = str(plan_bundle.get("task_mode") or task_mode)
    planned_plan = plan_bundle.get("plan") or []
    _db_update(job_id, {"plan": planned_plan, "task_mode": planned_task_mode})
    await _publish(
        job_id,
        {
            "type": "status_update",
            "status": JobStatus.WAITING_FOR_LLM.value,
            "step": 0,
            "tool": "planner",
            "task_mode": planned_task_mode,
            "plan": planned_plan,
        },
    )

    async def on_plan(plan: list[AgentStep], task_mode: str) -> None:
        plan_records = [step.model_dump(mode="json") for step in plan]
        _db_update(job_id, {"plan": plan_records, "task_mode": value_to_str(task_mode)})
        await _publish(
            job_id,
            {
                "type": "status_update",
                "status": JobStatus.WAITING_FOR_LLM.value,
                "step": 0,
                "tool": "planner",
                "task_mode": value_to_str(task_mode),
                "plan": plan_records,
            },
        )

    async def on_step_start(step_num: int, tool_name: str, args: dict[str, Any]) -> None:
        _db_update(job_id, {"status": JobStatus.RUNNING.value})
        await _publish(
            job_id,
            {
                "type": "step_start",
                "status": JobStatus.RUNNING.value,
                "step": step_num,
                "tool": tool_name,
                "args": args,
            },
        )

    async def on_log_chunk(step_num: int, tool_name: str, stream: str, chunk: str) -> None:
        # Production UX: never stream raw logs to the frontend.
        if not settings.DEBUG:
            return
        await _publish(
            job_id,
            {
                "type": "log_chunk",
                "status": JobStatus.RUNNING.value,
                "step": step_num,
                "tool": tool_name,
                "stream": stream,
                "data": chunk,
                "stdout_preview": "",
                "stderr_preview": "",
            },
        )

    async def on_step_result(result: StepResult) -> None:
        record = _step_to_record(result)
        accumulated_steps.append(record)
        _db_update(job_id, {"steps": accumulated_steps, "status": JobStatus.RUNNING.value})
        await _publish(
            job_id,
            {
                "type": "step_result",
                "status": JobStatus.RUNNING.value,
                "step": result.step,
                "tool": value_to_str(getattr(result, "tool", None)),
                "success": result.success,
                "exit_code": result.exit_code,
                "command": result.command,
                "command_type": result.command_type,
                "validation_passed": result.validation_passed,
                "step_status": result.status,
                "stdout_preview": "",
                "stderr_preview": "",
            },
        )

    async def on_decision(decision: AgentDecision) -> None:
        record = _decision_to_record(decision)
        accumulated_decisions.append(record)
        _db_update(job_id, {"decisions": accumulated_decisions, "status": JobStatus.WAITING_FOR_LLM.value})
        await _publish(
            job_id,
            {
                "type": "status_update",
                "status": JobStatus.WAITING_FOR_LLM.value,
                "step": len(accumulated_steps),
                "tool": "evaluator",
                "decision": record,
            },
        )

    try:
        plan_steps: list[AgentStep] | None = None
        if isinstance(planned_plan, list) and planned_plan:
            try:
                plan_steps = [AgentStep(**row) for row in planned_plan]
            except Exception:
                plan_steps = None

        resolved_workspace: dict[str, Any] | None = None
        if payload.workspace_id:
            try:
                resolved_workspace = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=user_id)
            except Exception:
                resolved_workspace = None
        if resolved_workspace is None:
            resolved_workspace = await WorkspaceService.create_workspace_from_prompt(
                user_id=user_id,
                server_id=payload.server_id,
                user_input=payload.objective,
            )
            payload.workspace_id = str(resolved_workspace.get("id") or "") or None
            _db_update(job_id, {"workspace_id": payload.workspace_id})

        forced_workspace_name = str(resolved_workspace.get("slug") or "").strip().lower() or _sanitize_workspace_name(payload.objective)
        forced_workspace_path = f"/root/workspaces/{forced_workspace_name}"
        if not forced_workspace_path.startswith("/root/workspaces"):
            forced_workspace_path = f"/root/workspaces/{_sanitize_workspace_name(payload.objective)}"
        os.makedirs(forced_workspace_path, exist_ok=True)
        if not payload.workspace_id:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="workspace_id missing")
        capabilities = await detect_capabilities(server)
        logger.info("Capabilities: %s", capabilities)
        RedisService.get_sync_client().set(f"ws:{payload.workspace_id}:capabilities", json.dumps(capabilities), ex=3600)
        await SSHService.execute(server=server, command=f"mkdir -p {shlex.quote(forced_workspace_path)}", command_timeout=step_timeout)
        logger.info("[workspace] name=%s | path=%s | job=%s", forced_workspace_name, forced_workspace_path, job_id)

        logger.info(
            "[agent] before_server_execution | job=%s | objective=%r | workspace_id=%s | server_id=%s | task_mode=%s | plan=%s",
            job_id,
            payload.objective,
            payload.workspace_id,
            payload.server_id,
            planned_task_mode,
            planned_plan,
        )
        loop_result = await run_server_execution(
            objective=payload.objective,
            intent=intent,
            task_mode=planned_task_mode,
            plan_steps=plan_steps,
            plan_context_summary=str(plan_bundle.get("context_summary") or ""),
            server=server,
            workspace_id=str(payload.workspace_id),
            workspace_path=forced_workspace_path,
            allow_write=allow_write,
            max_steps=payload.max_steps,
            step_timeout=step_timeout,
            conversation_history=conversation_history,
            memory=memory,
            on_step_start=on_step_start,
            on_step_result=on_step_result,
            on_log_chunk=on_log_chunk,
            on_plan=on_plan,
            on_decision=on_decision,
        )
        logger.info("[agent] server_execution_result | job=%s | success=%s | summary=%s | errors=%s", job_id, loop_result.success, loop_result.summary, loop_result.errors)
    except HTTPException as exc:
        obs.emit(
            level="ERROR",
            layer="execution",
            message="server_pipeline_failed_http",
            trace_id=trace_id,
            meta={"job_id": job_id, "error_type": "HTTPException", "detail": str(exc.detail)},
            exc_info=False,
        )
        logger.exception("[agent] server_pipeline_http_exception | job=%s", job_id, exc_info=exc)
        summary = _exception_to_error_string(exc)
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": summary})
        if payload.workspace_id:
            try:
                ChatService.save_workspace_message(
                    workspace_id=payload.workspace_id,
                    user_id=user_id,
                    role="assistant",
                    content=summary,
                )
            except Exception:
                logger.warning("Failed to persist assistant error message for job=%s", job_id)
        await _publish(job_id, {"type": "completed", "success": False, "summary": summary, "step": 0, "tool": None})
        return
    except Exception as exc:
        logger.exception("Unhandled error in agent loop (job=%s): %s", job_id, exc)
        summary = _exception_to_error_string(exc)
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": summary})
        await _publish(job_id, {"type": "completed", "success": False, "summary": summary, "step": 0, "tool": None})
        return

    final_status = JobStatus.COMPLETED if loop_result.success else JobStatus.FAILED
    final_summary = (
        _clean_user_summary(loop_result.summary, fallback=_GENERIC_FAILURE_MESSAGE)
        if loop_result.success
        else _stringify_error_detail(loop_result.errors[0]) if loop_result.errors else _stringify_error_detail(loop_result.summary)
    )
    logger.info("[router] input=%r | final_route=server | success=%s", (payload.objective or "")[:500], bool(loop_result.success))
    _db_update(
        job_id,
        {
            "status": final_status.value,
            "summary": final_summary,
            "intent": intent,
            "task_mode": value_to_str(loop_result.task_mode),
            "plan": [step.model_dump(mode="json") for step in loop_result.plan],
            "steps": accumulated_steps or [_step_to_record(step) for step in loop_result.steps],
            "decisions": accumulated_decisions or [_decision_to_record(decision) for decision in loop_result.decisions],
            "errors": loop_result.errors,
            "retries": loop_result.retries,
        },
    )
    if payload.workspace_id:
        try:
            ChatService.save_workspace_message(
                workspace_id=payload.workspace_id,
                user_id=user_id,
                role="assistant",
                content=final_summary,
            )
        except Exception:
            logger.warning("Failed to persist assistant summary for job=%s", job_id)
        try:
            await _memory_store.append(
                user_id=user_id,
                workspace_id=payload.workspace_id,
                item={
                    "intent": intent,
                    "task_mode": value_to_str(loop_result.task_mode),
                    "objective": payload.objective,
                    "plan": [step.model_dump(mode="json") for step in loop_result.plan],
                    "steps": (accumulated_steps or [_step_to_record(step) for step in loop_result.steps])[-8:],
                    "errors": (loop_result.errors or [])[-5:],
                    "retries": (loop_result.retries or [])[-5:],
                    "summary": loop_result.summary[:2000],
                    "success": bool(loop_result.success),
                },
            )
        except Exception:
            pass

    await _publish(
        job_id,
        {
            "type": "completed",
            "status": final_status.value,
            "success": loop_result.success,
            "summary": final_summary,
            "step": loop_result.steps_taken,
            "tool": None,
            "intent": intent,
            "task_mode": value_to_str(loop_result.task_mode),
        },
    )


async def _run_agent_loop(*, job_id: str, payload: JobCreate, user_id: str, trace_id: str | None = None) -> None:
    requested_mode = (value_to_str(getattr(payload, "mode", None)) or "").strip().lower() or "agent"
    token = set_request_mode(requested_mode)
    try:
        await run_agent_pipeline(job_id=job_id, payload=payload, user_id=user_id, trace_id=trace_id)
    finally:
        reset_request_mode(token)


class AgentService:
    @staticmethod
    def submit_job(user_id: str, payload: JobCreate, *, trace_id: str | None = None) -> JobAccepted:
        if payload.dry_run:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="dry_run is disabled for the production execution pipeline.",
            )
        # Workspace is resolved at execution time for code requests.
        if payload.workspace_id:
            workspace = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=user_id)
            if workspace["server_id"] != payload.server_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="workspace_id does not belong to the provided server_id",
                )
        return AgentService.create_job(user_id=user_id, payload=payload, trace_id=trace_id)

    @staticmethod
    def create_job(user_id: str, payload: JobCreate, *, trace_id: str | None = None) -> JobAccepted:
        allow_write = True
        payload.allow_write = True
        job_id = str(uuid4())
        trace_id = (trace_id or "").strip() or obs.new_trace_id()
        now = _now_iso()
        record: dict[str, Any] = {
            "id": job_id,
            "user_id": user_id,
            "workspace_id": payload.workspace_id,
            "server_id": payload.server_id,
            "objective": payload.objective,
            "status": JobStatus.QUEUED.value,
            "allow_write": bool(allow_write),
            "dry_run": payload.dry_run,
            # Safe defaults: classification will overwrite at runtime.
            "intent": "code",
            "task_mode": "simple",
            "plan": [],
            "steps": [],
            "decisions": [],
            "errors": [],
            "retries": [{"event": "trace_id", "trace_id": trace_id, "timestamp": now}],
            "summary": None,
            "created_at": now,
            "updated_at": now,
        }

        try:
            result = get_supabase().table(_TABLE).insert(record).execute()
        except APIError as exc:
            # Backward compatibility if DB migration hasn't been applied yet.
            msg = str(exc)
            if any(token in msg for token in ("intent", "errors", "retries")) and ("column" in msg or "field" in msg):
                fallback = dict(record)
                fallback.pop("intent", None)
                fallback.pop("errors", None)
                fallback.pop("retries", None)
                try:
                    result = get_supabase().table(_TABLE).insert(fallback).execute()
                except APIError as exc2:
                    logger.error("Failed to insert job row (fallback): %s", exc2)
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create job")
            else:
                logger.error("Failed to insert job row: %s", exc)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create job")

        if not result or not result.data:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create job")

        _local_subscribers[job_id] = set()
        _local_event_history[job_id] = []
        _local_event_seq[job_id] = 0
        return JobAccepted(id=job_id)

    @staticmethod
    def get_job(job_id: str, user_id: str) -> JobResponse:
        try:
            result = (
                get_supabase()
                .table(_TABLE)
                .select("*")
                .eq("id", job_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except APIError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

        if not result or not result.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        return _row_to_response(result.data)

    @staticmethod
    def list_jobs(user_id: str, workspace_id: str | None = None) -> list[JobResponse]:
        if not workspace_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})

        WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)
        query = get_supabase().table(_TABLE).select("*").eq("user_id", user_id).eq("workspace_id", workspace_id)
        result = query.order("created_at", desc=True).execute()
        return [_row_to_response(row) for row in result.data or []]

    @staticmethod
    async def get_event_history(job_id: str) -> list[dict[str, Any]]:
        redis = RedisService.get_async_client()
        if redis is not None:
            try:
                raw_items = await redis.lrange(f"job_events:{job_id}", 0, -1)
                return [json.loads(item) for item in raw_items]
            except Exception as exc:
                logger.warning("Failed to fetch Redis event history for job=%s: %s", job_id, exc)
        return list(_local_event_history.get(job_id, []))

    @staticmethod
    def subscribe(job_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        _local_subscribers.setdefault(job_id, set()).add(queue)
        return queue

    @staticmethod
    def unsubscribe(job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = _local_subscribers.get(job_id)
        if subscribers is None:
            return
        subscribers.discard(queue)

    @staticmethod
    async def run_job(job_id: str, payload: JobCreate, user_id: str, *, trace_id: str | None = None) -> None:
        async with _get_semaphore():
            await _run_agent_loop(job_id=job_id, payload=payload, user_id=user_id, trace_id=trace_id)

    @staticmethod
    async def run_explicit_mode(
        *,
        user_id: str,
        mode: str,
        objective: str,
        server_id: str | None = None,
        workspace_id: str | None = None,
        max_steps: int = 8,
        allow_write: bool | None = None,
        step_timeout_seconds: int | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Explicit-mode router entrypoint.

        mode:
          - "agent": deterministic template/tool + LLM fallback + guardrails + execution
          - "plan": LLM chat-only (no code generation, no execution)
          - "auto": optional advanced classifier routing
        """
        trace_id = (trace_id or "").strip() or obs.new_trace_id()
        selected = (mode or "").strip().lower()
        if not selected:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "mode_required"})

        if selected == "auto":
            intent = (await agent_llm.classify_intent(user_input=objective, conversation_history=None)).strip().lower()
            selected = "plan" if intent in {"chat"} else "agent"
            obs.emit(
                level="INFO",
                layer="router",
                message="mode_auto_selected",
                trace_id=trace_id,
                meta={"intent": intent, "mode": selected, "user_id": user_id},
            )
        else:
            obs.emit(
                level="INFO",
                layer="router",
                message="mode_selected",
                trace_id=trace_id,
                meta={"mode": selected, "user_id": user_id},
            )

        token = set_request_mode(selected)
        try:
            if selected == "plan":
                # Plan mode must be chat-only: no remote execution, no job creation, no workspace/server touching.
                message = await agent_llm.generate_chat_response(user_input=objective, conversation_history=[])
                return {"type": "chat", "message": message}

            if selected != "agent":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_mode", "mode": selected})

            if not server_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "server_id_required"})

            payload = JobCreate(
                workspace_id=workspace_id,
                server_id=server_id,
                objective=objective,
                max_steps=max_steps,
                allow_write=True if allow_write is None else bool(allow_write),
                dry_run=False,
                step_timeout_seconds=step_timeout_seconds,
            )
            accepted = AgentService.create_job(user_id=user_id, payload=payload, trace_id=trace_id)
            job_id = accepted.id

            settings = get_settings()
            step_timeout = payload.step_timeout_seconds or settings.AGENT_STEP_TIMEOUT

            _db_update(job_id, {"status": JobStatus.RUNNING.value})
            try:
                server = ServerService.get_server(server_id=payload.server_id, user_id=user_id)
            except HTTPException as exc:
                _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": str(exc.detail)})
                raise

            conversation_history2: list[dict[str, str]] = []
            if payload.workspace_id:
                try:
                    conversation_history2 = ChatService.get_recent_context_messages(
                        workspace_id=payload.workspace_id,
                        user_id=user_id,
                        limit=20,
                        current_input=payload.objective,
                    )
                except Exception:
                    conversation_history2 = []

            try:
                result = await _run_code_execution(
                    job_id=job_id,
                    payload=payload,
                    user_id=user_id,
                    server=server,
                    conversation_history=conversation_history2,
                    step_timeout=step_timeout,
                    trace_id=trace_id,
                )
            except HTTPException as exc:
                obs.emit(
                    level="ERROR",
                    layer="execution",
                    message="explicit_execution_failed_http",
                    trace_id=trace_id,
                    meta={"job_id": job_id, "error_type": "HTTPException", "detail": str(exc.detail)},
                )
                logger.exception("[agent] explicit_execution_http_exception | job=%s", job_id, exc_info=exc)
                result = {"type": "message", "message": _exception_to_error_string(exc), "success": False}
            except Exception as exc:
                obs.emit(
                    level="ERROR",
                    layer="execution",
                    message="explicit_execution_failed_exception",
                    trace_id=trace_id,
                    meta={"job_id": job_id, "error_type": type(exc).__name__, "detail": str(exc)},
                    exc_info=True,
                )
                logger.exception("[agent] explicit_execution_exception | job=%s", job_id)
                result = {"type": "message", "message": _exception_to_error_string(exc), "success": False}

            success = bool(result.get("success")) if isinstance(result, dict) else False
            workspace_url: str | None = None
            if payload.workspace_id:
                try:
                    ws = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=user_id)
                    workspace_url = str(ws.get("url") or "") or (f"https://{ws.get('domain')}" if ws.get("domain") else None)
                except Exception:
                    workspace_url = None

            if success:
                # BUG #2 guard — only emit URL after verified HTTP evidence in steps.
                steps_evidence = result.get("steps") or [] if isinstance(result, dict) else []
                if workspace_url and _deployment_verified_from_steps(steps_evidence):
                    message = f"All set. Open: {workspace_url}"
                    logger.info("[agent] deployment_url_verified | url=%s | job=%s", workspace_url, job_id)
                else:
                    message = _clean_user_summary(
                        str(result.get("summary") or result.get("message") or "") if isinstance(result, dict) else "",
                        fallback="All set.",
                    )
            else:
                message = _result_to_error_string(result)
            final_status = JobStatus.COMPLETED.value if success else JobStatus.FAILED.value
            _db_update(job_id, {"status": final_status, "summary": message})
            obs.METRICS.record_request(success=success, execution_time_seconds=0.0)
            return {"type": "message", "message": message, "success": success, "url": workspace_url}
        finally:
            reset_request_mode(token)


def to_forge_v2_response(job: JobResponse) -> dict[str, Any]:
    status_value = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
    raw_summary = job.summary or ""
    extracted = _extract_message_from_json_summary(raw_summary) or raw_summary
    safe_summary = extracted.strip() or _GENERIC_FAILURE_MESSAGE
    run: dict[str, Any] | None = {
        "agent": AgentTier.FORGE_V2.value,
        "job_id": job.id,
        "objective": job.objective,
        "dry_run": job.dry_run,
        "plan": job.plan or [],
        "results": _redact_step_outputs(job.steps or []),
        "decisions": [],
        "summary": safe_summary,
        "success": status_value == JobStatus.COMPLETED.value,
    }
    error: str | None = None

    if status_value == JobStatus.QUEUED.value:
        run = None
    elif status_value == JobStatus.FAILED.value:
        error = safe_summary

    return {
        "job_id": job.id,
        "status": status_value,
        "run": run,
        "error": error,
    }
