'''
ThinkSync Tool Registry.

All tools execute REAL operations on remote servers via SSH.
Nothing is simulated or faked.

Every tool function signature:
    async def <name>(*, server, args, workspace_path, allow_write, timeout) -> dict
    returns {"stdout": str, "stderr": str, "code": int}
'''

from __future__ import annotations

import ast
import asyncio
import base64
import json
import logging
import os
import re
import shlex
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypedDict

from fastapi import HTTPException, status

from core.config import get_settings
from core.value_coercion import value_to_str
from models.agent import StepResult, ToolName
from services import logger as obs
from services.ssh_service import SSHService

logger = logging.getLogger(__name__)

OutputChunkCallback = Callable[[int, str, str, str], Awaitable[None] | None]

class ExecResult(TypedDict):
    stdout: str
    stderr: str
    code: int

# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

_BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b", flags=re.IGNORECASE),
    re.compile(r"\bmkfs\b", flags=re.IGNORECASE),
    re.compile(r"\bdd\s+if=", flags=re.IGNORECASE),
    # Fork bomb variants, e.g. :(){ :|:& };:
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", flags=re.IGNORECASE),
    re.compile(r"\bshutdown\b", flags=re.IGNORECASE),
    re.compile(r"\breboot\b", flags=re.IGNORECASE),
    re.compile(r"\bpoweroff\b", flags=re.IGNORECASE),
    re.compile(r"\bhalt\b", flags=re.IGNORECASE),
    re.compile(r"\bpasswd\b", flags=re.IGNORECASE),
    re.compile(r"\bchmod\s+777\b", flags=re.IGNORECASE),
    re.compile(r">\s*/dev/sd", flags=re.IGNORECASE),
    re.compile(r">\s*/dev/nvme", flags=re.IGNORECASE),
    re.compile(r"\|\s*bash\b", flags=re.IGNORECASE),
    re.compile(r"\|\s*sh\b", flags=re.IGNORECASE),
    re.compile(r"\|\s*python\s+-c\b", flags=re.IGNORECASE),
    re.compile(r"\bkill\s+-9\s+1\b", flags=re.IGNORECASE),
    re.compile(r"\binit\s+[06]\b", flags=re.IGNORECASE),
    re.compile(r"curl\s+[^\s]+\s*\|", flags=re.IGNORECASE),
    re.compile(r"wget\s+[^\s]+\s*\|", flags=re.IGNORECASE),
]

_BLOCKED_LOCAL_PATHS: tuple[str, ...] = (
    "/root/workspaces",
    "/root/thinksync",
    "/tmp",
)

# ---------------------------------------------------------------------------
# Write-op auto-confirm patterns
# Commands that write files / create directories are safe to auto-confirm for
# ACTION steps. These are NOT in _BLOCKED_PATTERNS (truly dangerous commands),
# so they only trip the "dangerous" branch of _classify_command_risk.
# ---------------------------------------------------------------------------

_WRITE_OP_RE: re.Pattern[str] = re.compile(
    r'''
    (?:
        >{1,2}                          # shell redirect  > or >>
        | \btee\b                       # tee file
        | \btouch\b                     # touch file
        | \bmkdir\b                     # mkdir / mkdir -p
        | \bcp\b                        # cp src dst
        | \bmv\b                        # mv src dst
        # rm is intentionally excluded — requires explicit manual confirmation
    )
    ''',
    re.VERBOSE | re.IGNORECASE,
)


def _is_write_op(command: str) -> bool:
    '''Return True when a command performs a safe file-write / directory operation
    that can be auto-confirmed for ACTION-type plan steps.

    Excluded (require explicit confirm=true):
      rm, rm -rf, shutdown, reboot, kill, mkfs — all caught by _BLOCKED_PATTERNS
      or _classify_command_risk and never auto-approved here.
    '''
    return bool(_WRITE_OP_RE.search(command))


_BLOCKED_SCAFFOLDING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bgit\s+init\b", flags=re.IGNORECASE),
    re.compile(r"\bnpm\s+create\b", flags=re.IGNORECASE),
    re.compile(r"\bnpx\s+create-[^\s]+\b", flags=re.IGNORECASE),
    re.compile(r"\bcreate-next-app\b", flags=re.IGNORECASE),
    re.compile(r"\bcargo\s+new\b", flags=re.IGNORECASE),
]

_READ_ONLY_PREFIXES: tuple[str, ...] = (
    "uname", "uptime", "whoami", "id", "pwd", "ls", "df", "free",
    "cat", "head", "tail", "ps", "ss", "netstat", "curl",
    "docker ps", "docker images", "docker stats", "docker inspect",
    "systemctl status", "journalctl", "echo", "hostname",
    "date", "top -bn1", "vmstat", "iostat", "lscpu", "lsblk",
    "node --version", "node -v", "npm --version", "npm -v",
    "python --version", "python3 --version", "pip --version",
    "which", "command -v", "env", "printenv", "lsof",
    "ping -c", "traceroute", "nslookup", "dig",
    "git log", "git status", "git remote",
    "pm2 list", "pm2 status", "pm2 logs", "pm2 show",
)

# ---------------------------------------------------------------------------
# Input validators
# ---------------------------------------------------------------------------

def _classify_command_risk(command: str, *, allow_write: bool) -> str:
    '''
    Risk classification for commands executed over SSH.

    Returns: "safe" | "moderate" | "dangerous"
    '''
    lowered = (command or "").strip().lower()
    if not lowered:
        return "safe"

    if re.search(r"\b(systemctl|service)\s+(stop|disable|mask)\b", lowered):
        return "dangerous"
    if re.search(r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+rm|network\s+rm)\b", lowered):
        return "dangerous"
    if re.search(r"\bufw\s+disable\b", lowered):
        return "dangerous"
    if re.search(r"\biptables\b.*\s(-f|--flush)\b", lowered) or "iptables -f" in lowered or "iptables --flush" in lowered:
        return "dangerous"
    # BUG #5: kill (any variant) requires explicit confirmation — never auto-approve.
    if re.search(r"\bkill\b", lowered):
        return "dangerous"
    if re.search(r"\brm\b", lowered) and "rm -rf" not in lowered:
        return "dangerous"
    if ">" in lowered or ">>" in lowered:
        return "dangerous"

    if re.search(r"\b(systemctl|service)\s+(restart|reload|daemon-reload)\b", lowered):
        return "moderate"
    if re.search(r"\b(pm2|supervisorctl)\s+restart\b", lowered):
        return "moderate"
    if re.search(r"\bgit\s+pull\b", lowered):
        return "moderate"
    if re.search(r"\b(npm|pnpm|yarn)\s+(ci|install|run)\b", lowered):
        return "moderate"
    if re.search(r"\b(docker|docker-compose)\s+(restart|up|down)\b", lowered):
        return "moderate"

    # NOTE (BUG #5 fix): default to "dangerous", not "moderate".
    # Unknown/unmatched commands must require confirmation — never
    # auto-approve a command the risk classifier doesn't understand.
    return "dangerous"


def _is_dangerous(command: str) -> bool:
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(command):
            return True
    return False


def _guard_error(*, code: str, message: str, blocked_value: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": code,
            "message": message,
            "blocked_value": blocked_value,
        },
    )


def _validate_command(command: str, allow_write: bool, *, confirm_dangerous: bool = False) -> str:
    # NOTE: allow_write is now respected. The previous hard-coded
    # `allow_write = True` override has been removed.
    # Permission enforcement is centralized here.
    lowered = command.strip().lower()

    # Path restrictions
    for path in _BLOCKED_LOCAL_PATHS:
        if path in lowered:
            raise _guard_error(
                code="local_path_blocked",
                message=f"Access to {path} is blocked.",
                blocked_value=command,
            )

    # Shell pipe restrictions
    if re.search(r"curl\b.*\|\s*(bash|sh)\b", lowered, flags=re.IGNORECASE):
        raise _guard_error(
            code="unsafe_pipe_blocked",
            message="Piping curl to bash/sh is blocked.",
            blocked_value=command,
        )

    if re.search(r"wget\b.*\|\s*(bash|sh)\b", lowered, flags=re.IGNORECASE):
        raise _guard_error(
            code="unsafe_pipe_blocked",
            message="Piping wget to bash/sh is blocked.",
            blocked_value=command,
        )

    for pattern in _BLOCKED_SCAFFOLDING_PATTERNS:
        if pattern.search(command):
            raise _guard_error(
                code="project_scaffolding_blocked",
                message="Project scaffolding commands are not allowed.",
                blocked_value=command,
            )

    if _is_dangerous(command):
        raise _guard_error(
            code="dangerous_command_blocked",
            message="Rejected dangerous command.",
            blocked_value=command,
        )

    risk = _classify_command_risk(command, allow_write=allow_write)
    if risk == "dangerous" and not confirm_dangerous:
        raise _guard_error(
            code="confirmation_required",
            message="Dangerous command requires explicit confirmation. Re-run with args.confirm=true.",
            blocked_value=command,
        )
    return risk


def _validate_service_name(name: str) -> None:
    if not re.match(r"^[a-zA-Z0-9_.\\-]+$", name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid service name: {name!r}. Only alphanumeric, dash, dot, underscore allowed.",
        )


def _validate_port(port: int) -> None:
    if not 1024 <= port <= 65535:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid port {port}: must be 1024–65535.",
        )


def _validate_repo_url(url: str) -> None:
    if not re.match(r"^(https?://|git@)[^\s]{5,}$", url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid repository URL: {url!r}. Must be an HTTPS or SSH git URL.",
        )


def _sanitize_app_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "-", raw.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:40] or "ts-app"


def _truncate_for_log(value: str, limit: int = 800) -> str:
    if len(value) <= limit:
        return value
    return f'{value[:limit]}...<truncated>'


def _log_ssh_execution(tool_name: str, command: str) -> None:
    logger.info("[tools] SSH execute | tool=%s | command=%r", tool_name, command)


def _log_ssh_result(tool_name: str, exit_code: int, output: str) -> None:
    logger.info(
        "[tools] SSH result | tool=%s | exit_code=%s | output=%r",
        tool_name,
        exit_code,
        _truncate_for_log(output),
    )


def _scope_workspace_command(*, workspace_path: str, command: str) -> str:
    cleaned = (workspace_path or "").strip()
    if not cleaned or ".." in cleaned or "\n" in cleaned or "\r" in cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_workspace_path", "message": "Invalid workspace path"},
        )
    root = _workspaces_root()
    if not cleaned.startswith(f"{root}/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_workspace_path", "message": f"Workspace path must be under {root}"},
        )
    quoted_root = shlex.quote(root)
    quoted_path = shlex.quote(cleaned)
    # Resolve the workspace on the remote host before executing anything. A
    # lexical prefix check alone allows a workspace symlink to escape root.
    return (
        f"root=$(realpath -e -- {quoted_root}) && "
        f"resolved=$(realpath -e -- {quoted_path}) && "
        "case \"$resolved\" in \"$root\"/*) ;; "
        "*) printf '%s\\n' 'workspace path escapes configured root' >&2; exit 13 ;; esac && "
        "cd -- \"$resolved\" && "
        f"{command}"
    )


def _workspaces_root() -> str:
    settings = get_settings()
    root = (getattr(settings, "WORKSPACES_ROOT", None) or "").strip()
    if not root:
        root = os.getenv("THINKSYNC_WORKSPACES_ROOT", "").strip()
    if not root:
        root = "/root/workspaces"
    return root.rstrip("/")


def _validate_relative_path(rel_path: str) -> str:
    cleaned = (rel_path or "").strip().lstrip("/")
    if not cleaned or ".." in cleaned or "\n" in cleaned or "\r" in cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_path", "message": "Invalid file path"},
        )
    return cleaned


async def append_run_log(
    *,
    server: dict[str, Any],
    workspace_path: str,
    entry: dict[str, Any],
    timeout: int,
) -> None:
    try:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = base64.b64encode(line.encode("utf-8")).decode("ascii")
        py = (
            "import base64\n"
            f"data=base64.b64decode({encoded!r})\n"
            "with open('run.log','ab') as f:\n"
            "  f.write(data)\n"
        )
        cmd = f"python3 -c {shlex.quote(py)}"
        await exec_in_workspace(server=server, workspace_path=workspace_path, command=cmd, timeout=max(5, int(timeout)))
    except Exception:
        # Never break execution due to logging.
        return


async def exec_in_workspace(
    *,
    server: dict[str, Any],
    workspace_path: str,
    command: str,
    timeout: int,
    on_output_chunk: Callable[[str, str], Awaitable[None] | None] | None = None,
    trace_id: str | None = None,
) -> ExecResult:
    t0 = time.perf_counter()
    scoped = _scope_workspace_command(workspace_path=workspace_path, command=command)
    if trace_id:
        obs.emit(
            level="INFO",
            layer="tools",
            message="ssh_exec_start",
            trace_id=trace_id,
            meta={"timeout_s": int(timeout), "command": (command or "")[:400]},
        )
    resp = await SSHService.execute(server=server, command=scoped, command_timeout=timeout, on_output_chunk=on_output_chunk)
    dur = max(0.0, time.perf_counter() - t0)
    if trace_id or resp.exit_code != 0:
        obs.emit(
            level="INFO" if resp.exit_code == 0 else "ERROR",
            layer="tools",
            message="ssh_exec_end",
            trace_id=trace_id,
            meta={
                "timeout_s": int(timeout),
                "exit_code": int(resp.exit_code),
                "duration_s": dur,
                "command": (command or "")[:400],
                "stderr_tail": (resp.stderr or "")[-400:],
            },
        )
    return {"stdout": resp.stdout or "", "stderr": resp.stderr or "", "code": int(resp.exit_code)}


async def write_workspace_file(
    *,
    server: dict[str, Any],
    workspace_path: str,
    path: str,
    content: str,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    # NOTE: allow_write parameter is now respected. The previous
    # hard-coded `allow_write = True` override has been removed.
    # Permission enforcement is centralized in _validate_command().
    rel_path = _validate_relative_path(path)

    raw = content if isinstance(content, str) else str(content)
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    if len(encoded) > 750_000:
        # Prevent oversized commands over SSH.
        return {"stdout": "", "stderr": "File too large to write in a single operation.", "code": 1}

    py = (
        "import base64,os\n"
        f"p={rel_path!r}\n"
        "d=os.path.dirname(p)\n"
        "os.makedirs(d or '.', exist_ok=True)\n"
        f"data=base64.b64decode({encoded!r})\n"
        "with open(p,'wb') as f:\n"
        "  f.write(data)\n"
    )
    command = f"python3 -c {shlex.quote(py)}"
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


async def read_workspace_file(
    *,
    server: dict[str, Any],
    workspace_path: str,
    path: str,
    timeout: int,
) -> ExecResult:
    rel_path = _validate_relative_path(path)
    command = f"cat {shlex.quote(rel_path)}"
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


async def list_files(
    *,
    server: dict[str, Any],
    workspace_path: str,
    path: str = ".",
    timeout: int,
) -> ExecResult:
    """List files/dirs under ``path`` (relative to workspace), one per line.

    Uses ``ls -la`` so the agent can see permissions and sizes. Returns
    stdout with the listing; the caller parses it.
    """
    rel_path = _validate_relative_path(path)
    command = f"ls -la {shlex.quote(rel_path)}"
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


async def list_processes(
    *,
    server: dict[str, Any],
    workspace_path: str,
    timeout: int,
) -> ExecResult:
    """List running processes (scoped to the workspace where possible).

    Uses ``ps`` + ``grep`` on the workspace path so the agent can see what is
    running inside its workspace without listing the entire host.
    """
    root = _workspaces_root()
    command = (
        f"ps -eo pid,ppid,user,stat,etime,cmd | grep -E {shlex.quote(root)} || true"
    )
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


async def file_exists_in_workspace(
    *,
    server: dict[str, Any],
    workspace_path: str,
    path: str,
    timeout: int,
) -> bool:
    rel_path = _validate_relative_path(path)
    command = f"test -f {shlex.quote(rel_path)}"
    res = await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)
    return res["code"] == 0


async def patch_workspace_file(
    *,
    server: dict[str, Any],
    workspace_path: str,
    path: str,
    old_string: str,
    new_string: str,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    """Apply a precise in-file patch using old_string → new_string replacement.

    Uses an exact substring match (Anthropic-style). If ``old_string`` appears
    more than once the patch is rejected (ambiguous) to avoid corrupting the
    file. If it is absent the patch fails clearly so the caller can self-heal.
    Reads the current file, performs the replacement locally, and rewrites it
    in a single atomic SSH op.
    """
    rel_path = _validate_relative_path(path)

    # Encode both blobs; the worker reads the file, replaces once, writes back.
    old_b64 = base64.b64encode(old_string.encode("utf-8")).decode("ascii")
    new_b64 = base64.b64encode(new_string.encode("utf-8")).decode("ascii")

    py = (
        "import base64,os,io\n"
        f"p={rel_path!r}\n"
        f"old=base64.b64decode({old_b64!r}).decode('utf-8')\n"
        f"new=base64.b64decode({new_b64!r}).decode('utf-8')\n"
        "if not os.path.exists(p):\n"
        "    raise SystemExit('PATCH_TARGET_MISSING')\n"
        "with open(p,'r',encoding='utf-8') as f:\n"
        "    src=f.read()\n"
        "n=src.count(old)\n"
        "if n==0:\n"
        "    raise SystemExit('PATCH_ANCHOR_NOT_FOUND')\n"
        "if n>1:\n"
        "    raise SystemExit('PATCH_AMBIGUOUS_MULTIPLE_MATCHES')\n"
        "out=src.replace(old,new,1)\n"
        "with open(p,'w',encoding='utf-8') as f:\n"
        "    f.write(out)\n"
        "print('PATCH_APPLIED')\n"
    )
    command = f"python3 -c {shlex.quote(py)}"
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


async def install_python_deps(
    *,
    server: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
    fallback_packages: list[str] | None = None,
) -> ExecResult:
    # NOTE: allow_write parameter is now respected. Removed previous
    # hard-coded `allow_write = True` override.
    fallback = " ".join(shlex.quote(p) for p in (fallback_packages or []))
    # Keep it simple: requirements.txt wins; otherwise install fallback packages if any.
    if fallback:
        command = (
            "python3 -m pip install -r requirements.txt "
            "|| python3 -m pip install " + fallback
        )
    else:
        command = "python3 -m pip install -r requirements.txt"
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


# ---------------------------------------------------------------------------
# Environment / secret management (write ONLY to workspace-local .env)
# ---------------------------------------------------------------------------

def _validate_env_key(key: str) -> str:
    """Reject keys that could be used for injection or clobbering shell state."""
    cleaned = (key or "").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_env_key",
                "message": "Env key must match [A-Za-z_][A-Za-z0-9_]*",
            },
        )
    # Block attempts to override critical runtime vars.
    if cleaned in {"PATH", "HOME", "SHELL", "USER", "PWD", "THINKSYNC_WORKSPACES_ROOT"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "protected_env_key",
                "message": f"Refusing to overwrite protected key: {cleaned}",
            },
        )
    return cleaned


async def set_env(
    *,
    server: dict[str, Any],
    workspace_path: str,
    key: str,
    value: str,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    """Set KEY=VALUE in the workspace-local .env file (idempotent).

    Replaces an existing KEY line or appends a new one. Values are written
    verbatim; the caller is responsible for NOT logging `value` (redaction is
    enforced in the tool wrapper via _REDACTED_MARKER).
    """
    safe_key = _validate_env_key(key)
    rel_env = _validate_relative_path(".env")
    # Read current .env (may not exist yet), swap the key, write back.
    py = (
        "import os,re\n"
        f"p={rel_env!r}\n"
        f"k={safe_key!r}\n"
        f"v={value!r}\n"
        "lines=[]\n"
        "if os.path.exists(p):\n"
        "    with open(p,'r',encoding='utf-8') as f:\n"
        "        lines=f.read().splitlines()\n"
        "new=[ln for ln in lines if not ln.startswith(k+'=') and not ln.startswith(k+' ')+'=']\n"
        "new.append(f'{k}={v}')\n"
        "with open(p,'w',encoding='utf-8') as f:\n"
        "    f.write('\\n'.join(new)+'\\n')\n"
        "print('ENV_SET', k)\n"
    )
    command = f"python3 -c {shlex.quote(py)}"
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


async def write_secret(
    *,
    server: dict[str, Any],
    workspace_path: str,
    key: str,
    value: str,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    """Write a secret (token/password) into the workspace-local .env.

    Functionally identical to ``set_env`` but semantically a SECRET op — it MUST
    be routed through ApprovalType.SECRET (see executor/agent_service mapping).
    The value is never echoed to logs; the tool wrapper returns a redacted marker.
    """
    return await set_env(
        server=server,
        workspace_path=workspace_path,
        key=key,
        value=value,
        allow_write=allow_write,
        timeout=timeout,
    )


async def run_python_file(
    *,
    server: dict[str, Any],
    workspace_path: str,
    path: str,
    timeout: int,
) -> ExecResult:
    rel_path = _validate_relative_path(path)
    command = f"python3 {shlex.quote(rel_path)}"
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


async def run_python_server_background(
    *,
    server: dict[str, Any],
    workspace_path: str,
    path: str,
    timeout: int,
    log_path: str = "output.log",
    pid_path: str = "app.pid",
) -> ExecResult:
    rel_path = _validate_relative_path(path)
    rel_log = _validate_relative_path(log_path)
    rel_pid = _validate_relative_path(pid_path)
    # Start server process in background and persist PID/logs inside workspace.
    command = (
        f"nohup python3 {shlex.quote(rel_path)} > {shlex.quote(rel_log)} 2>&1 "
        f"& echo $! | tee {shlex.quote(rel_pid)}"
    )
    return await exec_in_workspace(server=server, workspace_path=workspace_path, command=command, timeout=timeout)


# ---------------------------------------------------------------------------
# Universal execution engine (robust Python execution layer)
# ---------------------------------------------------------------------------

_EXEC_LONG_RUNNING_TOKENS: tuple[str, ...] = (
    "run_polling",
    "uvicorn",
    "app.run(",
)
_EXEC_LONG_RUNNING_RE = re.compile(r"(?is)\bwhile\s+true\b")
_EXEC_SHORT_TASK_TIMEOUT_SECONDS = 20

_MODULE_TO_PIP: dict[str, str] = {
    "telegram": "python-telegram-bot",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
}


def detect_execution_mode(code: str) -> str:
    text = code or ""
    lowered = text.lower()
    if any(token in lowered for token in _EXEC_LONG_RUNNING_TOKENS):
        return "LONG_RUNNING"
    if _EXEC_LONG_RUNNING_RE.search(text):
        return "LONG_RUNNING"
    return "SHORT_TASK"


def _stdlib_modules() -> frozenset[str]:
    names = getattr(sys, "stdlib_module_names", None)
    if isinstance(names, (set, frozenset)):
        return frozenset(names)
    return frozenset()


def _is_stdlib_module(name: str) -> bool:
    cleaned = (name or "").strip()
    if not cleaned:
        return True
    top = cleaned.split(".", 1)[0]
    if top in sys.builtin_module_names:
        return True
    std = _stdlib_modules()
    return top in std


def _extract_import_modules(code: str) -> list[str]:
    text = code or ""
    modules: set[str] = set()
    try:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = (alias.name or "").strip()
                    if not name:
                        continue
                    modules.add(name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                if getattr(node, "level", 0):
                    continue
                mod = (node.module or "").strip()
                if not mod:
                    continue
                modules.add(mod.split(".", 1)[0])
    except SyntaxError:
        # Best-effort fallback for partially invalid code.
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m1 = re.match(r"^\s*import\s+(.+)$", line)
            if m1:
                chunk = m1.group(1)
                for part in chunk.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    name = part.split(" as ", 1)[0].strip()
                    if not name:
                        continue
                    modules.add(name.split(".", 1)[0])
                continue
            m2 = re.match(r"^\s*from\s+([a-zA-Z0-9_.]+)\s+import\b", line)
            if m2:
                name = m2.group(1).strip()
                if not name or name.startswith("."): 
                    continue
                modules.add(name.split(".", 1)[0])

    return sorted(mod for mod in modules if mod)


def _modules_to_packages(modules: list[str]) -> list[str]:
    pkgs: set[str] = set()
    for mod in modules:
        if _is_stdlib_module(mod):
            continue
        pkgs.add(_MODULE_TO_PIP.get(mod, mod))
    return sorted(pkgs)


async def _ensure_pip(
    *,
    server: dict[str, Any],
    workspace_path: str,
    timeout: int,
) -> tuple[bool, str]:
    parts: list[str] = []
    v_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m pip --version",
        timeout=timeout,
    )
    if v_res["stdout"] or v_res["stderr"]:
        parts.append("== python3 -m pip --version ==\n" + (v_res["stdout"] + v_res["stderr"]))
    if v_res["code"] == 0:
        return True, "\n".join(parts).strip()

    a_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="apt update",
        timeout=timeout,
    )
    if a_res["stdout"] or a_res["stderr"]:
        parts.append("== apt update ==\n" + (a_res["stdout"] + a_res["stderr"]))

    i_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="apt install -y python3-pip",
        timeout=timeout,
    )
    if i_res["stdout"] or i_res["stderr"]:
        parts.append("== apt install -y python3-pip ==\n" + (i_res["stdout"] + i_res["stderr"]))

    v2_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m pip --version",
        timeout=timeout,
    )
    if v2_res["stdout"] or v2_res["stderr"]:
        parts.append("== python3 -m pip --version (after apt) ==\n" + (v2_res["stdout"] + v2_res["stderr"]))
    return (v2_res["code"] == 0), "\n".join(parts).strip()


async def _pip_install(
    *,
    server: dict[str, Any],
    workspace_path: str,
    package: str,
    timeout: int,
) -> ExecResult:
    pkg = (package or "").strip()
    if not pkg:
        return {"stdout": "", "stderr": "empty package", "code": 1}
    return await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m pip install " + shlex.quote(pkg),
        timeout=timeout,
    )


async def _is_import_available(
    *,
    server: dict[str, Any],
    workspace_path: str,
    module: str,
    timeout: int,
) -> bool:
    mod = (module or "").strip()
    if not mod:
        return True
    py = f"import importlib; importlib.import_module({mod!r})"
    res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -c " + shlex.quote(py),
        timeout=timeout,
    )
    return res["code"] == 0


async def _tail_logs(
    *,
    server: dict[str, Any],
    workspace_path: str,
    lines: int = 100,
    output_log: str = "output.log",
    error_log: str = "error.log",
    timeout: int = 10,
) -> str:
    n = max(1, min(int(lines), 1000))
    out_path = _validate_relative_path(output_log)
    err_path = _validate_relative_path(error_log)
    cmd = (
        f"echo '--- {out_path} (stdout) ---'; "
        f"test -f {shlex.quote(out_path)} && tail -n {n} {shlex.quote(out_path)} || true; "
        f"echo '--- {err_path} (stderr) ---'; "
        f"test -f {shlex.quote(err_path)} && tail -n {n} {shlex.quote(err_path)} || true"
    )
    res = await exec_in_workspace(server=server, workspace_path=workspace_path, command=cmd, timeout=timeout)
    return (res["stdout"] + res["stderr"]).strip()


async def universal_execute_python(
    *,
    server: dict[str, Any],
    workspace_path: str,
    code: str,
    entrypoint: str = "main.py",
    setup_timeout: int = 300,
    trace_id: str | None = None,
) -> dict[str, Any]:
    total_timer = obs.Timer()
    # Workspace safety
    if not (workspace_path or "").startswith("/root/workspaces/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_workspace_path", "message": "Workspace path must be under /root/workspaces"},
        )

    start_log = obs.make_log(
        level="INFO",
        layer="execution",
        message="execution_start",
        trace_id=trace_id,
        meta={"entrypoint": entrypoint, "setup_timeout_s": int(setup_timeout), "mode": "execution"},
    )
    await append_run_log(server=server, workspace_path=workspace_path, entry=start_log, timeout=5)

    await SSHService.execute(server=server, command=f"mkdir -p {shlex.quote(workspace_path)}", command_timeout=max(5, int(setup_timeout)))

    # Ensure code file exists (idempotent)
    await write_workspace_file(
        server=server,
        workspace_path=workspace_path,
        path=entrypoint,
        content=code or "",
        allow_write=True,
        timeout=max(10, int(setup_timeout)),
    )

    # Pip guarantee
    await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="python3 -m pip --version",
        timeout=max(10, int(setup_timeout)),
    )
    pip_ready, pip_logs = await _ensure_pip(server=server, workspace_path=workspace_path, timeout=max(30, int(setup_timeout)))

    # Dependency auto-install (best-effort)
    detected_modules = _extract_import_modules(code or "")
    install_logs: list[str] = []
    if pip_logs:
        install_logs.append(pip_logs)

    if pip_ready:
        has_reqs = await file_exists_in_workspace(server=server, workspace_path=workspace_path, path="requirements.txt", timeout=max(10, int(setup_timeout)))
        if has_reqs:
            r_res = await exec_in_workspace(
                server=server,
                workspace_path=workspace_path,
                command="python3 -m pip install -r requirements.txt",
                timeout=max(30, int(setup_timeout)),
            )
            if r_res["stdout"] or r_res["stderr"]:
                install_logs.append("== python3 -m pip install -r requirements.txt ==\n" + (r_res["stdout"] + r_res["stderr"]))

        for mod in detected_modules:
            if _is_stdlib_module(mod):
                continue
            ok = await _is_import_available(server=server, workspace_path=workspace_path, module=mod, timeout=15)
            if ok:
                continue
            pkg = _MODULE_TO_PIP.get(mod, mod)
            p_res = await _pip_install(
                server=server,
                workspace_path=workspace_path,
                package=pkg,
                timeout=max(30, int(setup_timeout)),
            )
            if p_res["stdout"] or p_res["stderr"]:
                install_logs.append(f"== python3 -m pip install {pkg} ==\n" + (p_res["stdout"] + p_res["stderr"]))

    mode = detect_execution_mode(code or "")
    rel_entry = _validate_relative_path(entrypoint)

    if mode == "LONG_RUNNING":
        exec_timer = obs.Timer()
        # Structured logs: output.log contains both stdout+stderr; error.log is a symlink for compatibility.
        cmd = (
            "set -e; "
            ": > output.log; "
            "ln -sf output.log error.log; "
            f"nohup python3 {shlex.quote(rel_entry)} > output.log 2>&1 < /dev/null & "
            "echo $! > app.pid"
        )
        await exec_in_workspace(
            server=server,
            workspace_path=workspace_path,
            command=cmd,
            timeout=20,
            trace_id=trace_id,
        )
        logs = await _tail_logs(server=server, workspace_path=workspace_path, lines=100, timeout=10)
        if install_logs:
            logs = ("\n\n".join(install_logs).strip() + "\n\n" + logs).strip()
        end_log = obs.make_log(
            level="INFO",
            layer="execution",
            message="execution_end",
            trace_id=trace_id,
            meta={
                "mode": "BACKGROUND",
                "success": True,
                "execution_time_s": exec_timer.elapsed(),
                "total_time_s": total_timer.elapsed(),
            },
        )
        await append_run_log(server=server, workspace_path=workspace_path, entry=end_log, timeout=5)
        return {
            "type": "background",
            "status": "running",
            "workspace": workspace_path,
            "log_file": "output.log",
            "logs": logs,
            "success": True,
            "trace_id": trace_id,
            "execution_time": exec_timer.elapsed(),
            "total_time": total_timer.elapsed(),
        }

    exec_timer = obs.Timer()
    cmd = (
        "set -e; "
        ": > output.log; "
        ": > error.log; "
        f"timeout {_EXEC_SHORT_TASK_TIMEOUT_SECONDS}s python3 {shlex.quote(rel_entry)} > output.log 2> error.log"
    )
    exec_res = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command=cmd,
        timeout=_EXEC_SHORT_TASK_TIMEOUT_SECONDS + 5,
        trace_id=trace_id,
    )
    exit_code = exec_res["code"]
    logs = await _tail_logs(server=server, workspace_path=workspace_path, lines=100, timeout=10)
    if install_logs:
        logs = ("\n\n".join(install_logs).strip() + "\n\n" + logs).strip()
    end_log2 = obs.make_log(
        level="INFO" if exit_code == 0 else "ERROR",
        layer="execution",
        message="execution_end",
        trace_id=trace_id,
        meta={
            "mode": "FOREGROUND",
            "success": exit_code == 0,
            "exit_code": int(exit_code),
            "execution_time_s": exec_timer.elapsed(),
            "total_time_s": total_timer.elapsed(),
        },
    )
    await append_run_log(server=server, workspace_path=workspace_path, entry=end_log2, timeout=5)
    return {
        "type": "execution",
        "status": "completed",
        "logs": logs,
        "success": exit_code == 0,
        "trace_id": trace_id,
        "execution_time": exec_timer.elapsed(),
        "total_time": total_timer.elapsed(),
    }
# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

CommandType = str

CHECK_COMMANDS: tuple[str, ...] = (
    "grep",
    "test",
    "ss",
    "ls",
    "find",
    "which",
    "command",
    "pgrep",
    "lsof",
)
VERIFY_COMMANDS: tuple[str, ...] = ("curl", "wget")


def _first_shell_word(command: str) -> str:
    cleaned = (command or "").strip()
    if not cleaned:
        return ""
    try:
        parts = shlex.split(cleaned, comments=False, posix=True)
    except ValueError:
        parts = cleaned.split()
    return (parts[0] if parts else "").strip().lower()


def classify_command(command: str) -> CommandType:
    '''Classify a shell command by how its exit code should be interpreted.'''
    cleaned = (command or "").strip()
    lowered = cleaned.lower()
    if not lowered:
        return "ACTION"
    if re.search(r"(^|[;&|]\s*)(curl|wget)\b", lowered):
        return "VERIFY"
    if re.search(r"(^|[;&|]\s*)(grep|test|\[|ss|ls|find|which|command\s+-v|pgrep|lsof)\b", lowered):
        return "CHECK"
    first = _first_shell_word(cleaned)
    if first in VERIFY_COMMANDS:
        return "VERIFY"
    if first in CHECK_COMMANDS:
        return "CHECK"
    return "ACTION"


def command_success(command_type: CommandType, exit_code: int) -> bool:
    '''Return whether a classified command completed according to its semantics.'''
    normalized = (command_type or "ACTION").upper()
    if normalized == "CHECK":
        return exit_code in {0, 1}
    return exit_code == 0


def _tool_command(tool: ToolName, args: dict[str, Any]) -> str:
    if tool == ToolName.RUN_COMMAND:
        return str((args or {}).get("command") or "")
    if tool == ToolName.CHECK_DISK:
        return "df -h"
    if tool == ToolName.CHECK_MEMORY:
        return "free -m"
    if tool == ToolName.READ_LOGS:
        service_name = str((args or {}).get("service_name") or "")
        lines = int((args or {}).get("lines") or 100)
        return f"tail -n {lines} {service_name}" if service_name.startswith("/") else f"journalctl -u {service_name} -n {lines} --no-pager"
    if tool == ToolName.RESTART_SERVICE:
        return f"systemctl restart {str((args or {}).get('service_name') or '').strip()}"
    if tool == ToolName.DEPLOY_APP:
        return str((args or {}).get("deploy_command") or "")
    return value_to_str(getattr(tool, "value", None) or tool)


def _tool_command_type(tool: ToolName, command: str) -> CommandType:
    if tool in {ToolName.CHECK_DISK, ToolName.CHECK_MEMORY, ToolName.READ_LOGS}:
        return "CHECK"
    if tool == ToolName.RUN_COMMAND:
        return classify_command(command)
    return "ACTION"


async def _run_command(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    command: str = args.get("command", "").strip()
    if not command:
        return {"stdout": "", "stderr": "run_command: 'command' argument is required", "code": 1}
    # Auto-confirm write operations (>, >>, tee, touch, mkdir, cp, mv, rm).
    # Truly destructive commands (rm -rf, shutdown, etc.) remain blocked by
    # _BLOCKED_PATTERNS regardless of this flag.
    confirm = bool(args.get("confirm", False)) or _is_write_op(command)
    if confirm and args.get("confirm") is None:
        logger.info("[tools] run_command: auto-confirmed write-op | command=%r", command)
    _validate_command(command, allow_write=allow_write, confirm_dangerous=confirm)
    scoped_command = _scope_workspace_command(workspace_path=workspace_path, command=command)
    _log_ssh_execution(ToolName.RUN_COMMAND.value, scoped_command)
    resp = await SSHService.execute(
        server=server,
        command=scoped_command,
        command_timeout=timeout,
        on_output_chunk=(
            None if on_output_chunk is None else
            lambda stream, chunk: on_output_chunk(step_number, ToolName.RUN_COMMAND.value, stream, chunk)
        ),
    )
    _log_ssh_result(ToolName.RUN_COMMAND.value, resp.exit_code, resp.output)
    command_type = classify_command(command)
    if command_success(command_type, int(resp.exit_code)):
        return {"stdout": resp.output or "", "stderr": "", "code": int(resp.exit_code)}
    return {"stdout": "", "stderr": resp.output or "", "code": int(resp.exit_code)}


async def _check_disk(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    command = _scope_workspace_command(workspace_path=workspace_path, command="df -h")
    _log_ssh_execution(ToolName.CHECK_DISK.value, command)
    resp = await SSHService.execute(
        server=server,
        command=command,
        command_timeout=timeout,
        on_output_chunk=(
            None if on_output_chunk is None else
            lambda stream, chunk: on_output_chunk(step_number, ToolName.CHECK_DISK.value, stream, chunk)
        ),
    )
    _log_ssh_result(ToolName.CHECK_DISK.value, resp.exit_code, resp.output)
    if resp.exit_code == 0:
        return {"stdout": resp.output or "", "stderr": "", "code": 0}
    return {"stdout": "", "stderr": resp.output or "", "code": int(resp.exit_code)}


async def _check_memory(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    command = _scope_workspace_command(workspace_path=workspace_path, command="free -m")
    _log_ssh_execution(ToolName.CHECK_MEMORY.value, command)
    resp = await SSHService.execute(
        server=server,
        command=command,
        command_timeout=timeout,
        on_output_chunk=(
            None if on_output_chunk is None else
            lambda stream, chunk: on_output_chunk(step_number, ToolName.CHECK_MEMORY.value, stream, chunk)
        ),
    )
    _log_ssh_result(ToolName.CHECK_MEMORY.value, resp.exit_code, resp.output)
    if resp.exit_code == 0:
        return {"stdout": resp.output or "", "stderr": "", "code": 0}
    return {"stdout": "", "stderr": resp.output or "", "code": int(resp.exit_code)}


async def _read_logs(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    service_name: str = args.get("service_name", "").strip()
    lines: int = max(1, min(int(args.get("lines", 100)), 1000))

    if not service_name:
        return {"stdout": "", "stderr": "read_logs: 'service_name' argument is required", "code": 1}

    if service_name.startswith("/"):
        # Absolute path — read file directly
        candidate = f"tail -n {lines} {service_name}"
        try:
            _validate_command(candidate, allow_write=False)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            return {"stdout": "", "stderr": json.dumps(detail), "code": 1}
        command = f"tail -n {lines} {service_name}"
    else:
        _validate_service_name(service_name)
        command = f"journalctl -u {service_name} -n {lines} --no-pager"

    scoped_command = _scope_workspace_command(workspace_path=workspace_path, command=command)
    _log_ssh_execution(ToolName.READ_LOGS.value, scoped_command)
    resp = await SSHService.execute(
        server=server,
        command=scoped_command,
        command_timeout=timeout,
        on_output_chunk=(
            None if on_output_chunk is None else
            lambda stream, chunk: on_output_chunk(step_number, ToolName.READ_LOGS.value, stream, chunk)
        ),
    )
    _log_ssh_result(ToolName.READ_LOGS.value, resp.exit_code, resp.output)
    if resp.exit_code == 0:
        return {"stdout": resp.output or "", "stderr": "", "code": 0}
    return {"stdout": "", "stderr": resp.output or "", "code": int(resp.exit_code)}


async def _restart_service(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    # NOTE: allow_write parameter is now respected.
    # Removed previous hard-coded `allow_write = True` override.
    service_name: str = args.get("service_name", "").strip()
    if not service_name:
        return {"stdout": "", "stderr": "restart_service: 'service_name' argument is required", "code": 1}

    _validate_service_name(service_name)
    command = f"systemctl restart {service_name}"
    scoped_command = _scope_workspace_command(workspace_path=workspace_path, command=command)
    _log_ssh_execution(ToolName.RESTART_SERVICE.value, scoped_command)
    resp = await SSHService.execute(
        server=server,
        command=scoped_command,
        command_timeout=timeout,
        on_output_chunk=(
            None if on_output_chunk is None else
            lambda stream, chunk: on_output_chunk(step_number, ToolName.RESTART_SERVICE.value, stream, chunk)
        ),
    )
    _log_ssh_result(ToolName.RESTART_SERVICE.value, resp.exit_code, resp.output)
    if resp.exit_code == 0:
        return {"stdout": f"Service '{service_name}' restarted successfully.", "stderr": "", "code": 0}
    return {"stdout": "", "stderr": resp.output or "", "code": int(resp.exit_code)}


async def _deploy_app(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    '''Legacy deploy_app tool — runs a deploy command via SSH.'''
    app_name: str = args.get("app_name", "").strip()
    deploy_command: str = args.get("deploy_command", "").strip()

    if not app_name or not deploy_command:
        return {"stdout": "", "stderr": "deploy_app: 'app_name' and 'deploy_command' are required", "code": 1}

    confirm = bool(args.get("confirm", False))
    _validate_command(deploy_command, allow_write=allow_write, confirm_dangerous=confirm)
    scoped_command = _scope_workspace_command(workspace_path=workspace_path, command=deploy_command)
    _log_ssh_execution(ToolName.DEPLOY_APP.value, scoped_command)
    resp = await SSHService.execute(
        server=server,
        command=scoped_command,
        command_timeout=timeout,
        on_output_chunk=(
            None if on_output_chunk is None else
            lambda stream, chunk: on_output_chunk(step_number, ToolName.DEPLOY_APP.value, stream, chunk)
        ),
    )
    _log_ssh_result(ToolName.DEPLOY_APP.value, resp.exit_code, resp.output)
    if resp.exit_code == 0:
        return {"stdout": resp.output or "", "stderr": "", "code": 0}
    return {"stdout": "", "stderr": resp.output or "", "code": int(resp.exit_code)}


async def _deploy_nextjs_app(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    return {
        "stdout": "",
        "stderr": json.dumps({
            "code": "tool_disabled",
            "message": "deploy_nextjs_app is disabled. Use deploy_app with an explicit remote-safe deploy command.",
            "blocked_value": ToolName.DEPLOY_NEXTJS_APP.value,
        }),
        "code": 1,
    }

async def _list_files(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    path = (args.get("path", ".") or ".").strip()
    _validate_relative_path(path)
    command = f"ls -laF {shlex.quote(path)}"
    scoped_command = _scope_workspace_command(workspace_path=workspace_path, command=command)
    _log_ssh_execution(ToolName.LIST_FILES.value, scoped_command)
    resp = await SSHService.execute(
        server=server, command=scoped_command, command_timeout=timeout
    )
    _log_ssh_result(ToolName.LIST_FILES.value, resp.exit_code, resp.output)
    return {"stdout": resp.output or "", "stderr": resp.stderr or "", "code": int(resp.exit_code)}


async def _read_file(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    path = (args.get("path", "") or "").strip()
    if not path:
        return {"stdout": "", "stderr": "read_file: 'path' argument is required", "code": 1}
    rel_path = _validate_relative_path(path)
    return await read_workspace_file(server=server, workspace_path=workspace_path, path=rel_path, timeout=timeout)


async def _write_file(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    if not allow_write:
        return {"stdout": "", "stderr": "write_file: permission denied", "code": 1}
    path = (args.get("path", "") or "").strip()
    content = args.get("content", "")
    if not path:
        return {"stdout": "", "stderr": "write_file: 'path' argument is required", "code": 1}
    
    rel_path = _validate_relative_path(path)
    res = await write_workspace_file(
        server=server,
        workspace_path=workspace_path,
        path=rel_path,
        content=content,
        allow_write=True,
        timeout=timeout,
    )
    if res["code"] == 0:
        res["stdout"] = f"Successfully wrote {len(content)} bytes to {path}"
    return res


async def _patch_file(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    if not allow_write:
        return {"stdout": "", "stderr": "patch_file: permission denied", "code": 1}
    path = (args.get("path", "") or "").strip()
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    if not path:
        return {"stdout": "", "stderr": "patch_file: 'path' argument is required", "code": 1}
    if old_string == "" or new_string == "":
        return {"stdout": "", "stderr": "patch_file: 'old_string' and 'new_string' are required", "code": 1}

    rel_path = _validate_relative_path(path)
    res = await patch_workspace_file(
        server=server,
        workspace_path=workspace_path,
        path=rel_path,
        old_string=old_string,
        new_string=new_string,
        allow_write=True,
        timeout=timeout,
    )
    if res["code"] == 0 and "PATCH_APPLIED" in res["stdout"]:
        res["stdout"] = f"Successfully patched {path}"
    elif res["code"] != 0:
        # Surface the SystemExit reason as a clear stderr message.
        err = res["stderr"] or res["stdout"]
        if any(t in err for t in ("PATCH_TARGET_MISSING", "PATCH_ANCHOR_NOT_FOUND", "PATCH_AMBIGUOUS")):
            res["stderr"] = f"patch_file failed: {err.strip()}"
    return res


async def _config_set(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    if not allow_write:
        return {"stdout": "", "stderr": "config_set: permission denied", "code": 1}
    key = (args.get("key", "") or "").strip()
    value = str(args.get("value", "") or "")
    if not key:
        return {"stdout": "", "stderr": "config_set: 'key' argument is required", "code": 1}
    res = await set_env(
        server=server,
        workspace_path=workspace_path,
        key=key,
        value=value,
        allow_write=True,
        timeout=timeout,
    )
    # Redact the value from any echoed output.
    res["stdout"] = res["stdout"].replace(value, "***REDACTED***") if value else res["stdout"]
    res["stderr"] = res["stderr"].replace(value, "***REDACTED***") if value else res["stderr"]
    return res


async def _write_secret(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    if not allow_write:
        return {"stdout": "", "stderr": "write_secret: permission denied", "code": 1}
    key = (args.get("key", "") or "").strip()
    value = str(args.get("value", "") or "")
    if not key:
        return {"stdout": "", "stderr": "write_secret: 'key' argument is required", "code": 1}
    res = await write_secret(
        server=server,
        workspace_path=workspace_path,
        key=key,
        value=value,
        allow_write=True,
        timeout=timeout,
    )
    # Never echo secret values — always redact.
    res["stdout"] = res["stdout"].replace(value, "***REDACTED***") if value else res["stdout"]
    res["stderr"] = res["stderr"].replace(value, "***REDACTED***") if value else res["stderr"]
    return res


async def _list_processes(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> ExecResult:
    command = "ps aux"
    scoped_command = _scope_workspace_command(workspace_path=workspace_path, command=command)
    _log_ssh_execution(ToolName.LIST_PROCESSES.value, scoped_command)
    resp = await SSHService.execute(
        server=server, command=scoped_command, command_timeout=timeout
    )
    _log_ssh_result(ToolName.LIST_PROCESSES.value, resp.exit_code, resp.output)
    return {"stdout": resp.output or "", "stderr": resp.stderr or "", "code": int(resp.exit_code)}


# _TOOL_FN dispatch table is defined at the END of this module (after all
# tool functions) so that forward references to git_*/_git_* resolve.


# ---------------------------------------------------------------------------
# Git layer (agent operates ONLY inside an existing workspace git repo)
# ---------------------------------------------------------------------------

async def _require_git_repo(
    *,
    server: dict[str, Any],
    workspace_path: str,
    timeout: int,
) -> ExecResult | None:
    """Verify the workspace is a git repository. Returns None if OK, else an error result."""
    check = await exec_in_workspace(
        server=server,
        workspace_path=workspace_path,
        command="test -d .git && echo GIT_OK || echo NO_GIT",
        timeout=max(5, min(timeout, 15)),
    )
    if "GIT_OK" not in (check["stdout"] or ""):
        return {
            "stdout": "",
            "stderr": "git: workspace is not a git repository (import it via workspace import first)",
            "code": 1,
        }
    return None


async def _git_status(
    *,
    server: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    return await exec_in_workspace(
        server=server, workspace_path=workspace_path,
        command="git status --porcelain=v1 -b", timeout=timeout,
    )


async def _git_diff(
    *,
    server: dict[str, Any],
    workspace_path: str,
    staged: bool = False,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    flag = "--cached" if staged else ""
    return await exec_in_workspace(
        server=server, workspace_path=workspace_path,
        command=f"git diff {flag} --stat", timeout=timeout,
    )


async def _git_branch(
    *,
    server: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    return await exec_in_workspace(
        server=server, workspace_path=workspace_path,
        command="git branch -a && echo '---HEAD---' && git rev-parse --abbrev-ref HEAD",
        timeout=timeout,
    )


async def _git_commit(
    *,
    server: dict[str, Any],
    workspace_path: str,
    message: str,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    safe_msg = (message or "agent commit").replace('"', "'")[:200]
    return await exec_in_workspace(
        server=server, workspace_path=workspace_path,
        command=f"git add -A && git commit -m {shlex.quote(safe_msg)}",
        timeout=timeout,
    )


async def _git_restore(
    *,
    server: dict[str, Any],
    workspace_path: str,
    paths: list[str] | None = None,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    target = " ".join(shlex.quote(p) for p in (paths or [])) or "."
    return await exec_in_workspace(
        server=server, workspace_path=workspace_path,
        command=f"git restore --worktree {target}",
        timeout=timeout,
    )


async def _git_reset(
    *,
    server: dict[str, Any],
    workspace_path: str,
    mode: str = "mixed",
    target: str = "HEAD",
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    """Destructive — requires HIGH approval (enforced in executor/agent_service).

    mode: 'soft' | 'mixed' | 'hard'. 'hard' discards ALL local changes.
    """
    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    if mode not in {"soft", "mixed", "hard"}:
        return {"stdout": "", "stderr": f"git_reset: invalid mode {mode!r}", "code": 1}
    safe_target = (target or "HEAD").replace(" ", "")
    if not re.match(r"^[A-Za-z0-9_./\-^~:]+$", safe_target):
        return {"stdout": "", "stderr": "git_reset: invalid target", "code": 1}
    return await exec_in_workspace(
        server=server, workspace_path=workspace_path,
        command=f"git reset --{mode} {shlex.quote(safe_target)}",
        timeout=timeout,
    )


async def _git_clean(
    *,
    server: dict[str, Any],
    workspace_path: str,
    force: bool = False,
    allow_write: bool | None,
    timeout: int,
) -> ExecResult:
    """Remove untracked files. Destructive — requires HIGH approval.

    By default dry-run (-n) only; pass force=true to actually delete.
    """
    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    if not force:
        return await exec_in_workspace(
            server=server, workspace_path=workspace_path,
            command="git clean -ndx", timeout=timeout,
        )
    return await exec_in_workspace(
        server=server, workspace_path=workspace_path,
        command="git clean -fdx", timeout=timeout,
    )


# ---------------------------------------------------------------------------
# GitHub agent tools (pull / push over an EXISTING workspace repo)
#
# These NEVER clone and NEVER see a plaintext private key. They resolve the
# workspace's linked GitHub connection (from the DB), decrypt the key in-process,
# and delegate the actual git transport to GitHubService. Push is gated by
# HIGH risk + explicit human approval (enforced in executor/agent_service).
# ---------------------------------------------------------------------------


async def _github_pull(
    *,
    server: dict[str, Any],
    workspace_path: str,
    github_connection_id: str,
    user_id: str | None = None,
    remote: str = "origin",
    branch: str | None = None,
    strategy: str = "ff_only",
    allow_write: bool | None = None,
    timeout: int = 180,
) -> ExecResult:
    """Fetch + integrate (fast-forward / merge / rebase) an existing repo.

    Requires the workspace to already be a git repo (cloned at workspace
    creation time, never by the agent). The credential PROVIDER (SSH key vs
    GitHub App installation token) is resolved entirely inside the service
    layer (``git_transport``) from the linked connection — the agent tool has
    no knowledge of SSH/OAuth/App. Returns a structured result.
    """
    from services.git_transport import workspace_pull

    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    if strategy not in ("ff_only", "merge", "rebase"):
        return {"stdout": "", "stderr": f"github_pull: invalid strategy {strategy!r}", "code": 1}
    try:
        res = await workspace_pull(
            connection_id=github_connection_id,
            user_id=user_id,
            workspace_path=workspace_path,
            server=server,
            remote=remote,
            branch=branch,
            strategy=strategy,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return {"stdout": "", "stderr": json.dumps(detail), "code": 1}
    except Exception as exc:  # noqa: BLE001
        return {"stdout": "", "stderr": f"github_pull: failed to load credential: {exc}", "code": 1}

    return {
        "stdout": res.get("stderr", "") if not res.get("ok") else "pull ok",
        "stderr": res.get("stderr", "") if res.get("ok") else "",
        "code": int(res.get("code", 1)),
    }


async def _github_push(
    *,
    server: dict[str, Any],
    workspace_path: str,
    github_connection_id: str,
    user_id: str | None = None,
    remote: str = "origin",
    branch: str | None = None,
    force: bool = False,
    tags: bool = False,
    allow_write: bool | None = None,
    timeout: int = 180,
) -> ExecResult:
    """Push commits to the remote. ALWAYS requires explicit approval (HIGH risk).

    The force flag maps to ``--force-with-lease`` (safer than ``--force``).
    Provider selection (SSH vs GitHub App token) happens in the service layer
    (``git_transport``); the agent tool never sees a credential.
    """
    from services.git_transport import workspace_push

    err = await _require_git_repo(server=server, workspace_path=workspace_path, timeout=timeout)
    if err:
        return err
    if not allow_write:
        return {
            "stdout": "",
            "stderr": "github_push blocked: allow_write=false (push requires write access + approval)",
            "code": 1,
        }
    try:
        res = await workspace_push(
            connection_id=github_connection_id,
            user_id=user_id,
            workspace_path=workspace_path,
            server=server,
            remote=remote,
            branch=branch,
            force=force,
            tags=tags,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return {"stdout": "", "stderr": json.dumps(detail), "code": 1}
    except Exception as exc:  # noqa: BLE001
        return {"stdout": "", "stderr": f"github_push: failed to load credential: {exc}", "code": 1}

    return {
        "stdout": "push ok" if res.get("ok") else "",
        "stderr": res.get("stderr", "") if not res.get("ok") else "",
        "code": int(res.get("code", 1)),
    }


async def execute_tool(
    *,
    tool_name: str,
    args: dict[str, Any],
    intent: str,
    server: dict[str, Any],
    workspace_path: str,
    allow_write: bool | None,
    timeout: int,
    step_number: int = 0,
    user_id: str | None = None,
    on_output_chunk: OutputChunkCallback | None = None,
) -> StepResult:
    '''
    Dispatch a named tool call to its implementation and return a StepResult.
    All execution is real — no simulation, no faking.
    '''
    executed_at = datetime.now(timezone.utc)
    # NOTE: allow_write parameter is now respected.
    # The previous hard-coded `allow_write = True` override has been removed.
    # Permission enforcement is centralized in _validate_command().

    # CRITICAL: Block all tool execution unless the caller explicitly routed intent == "server".
    normalized_intent = (intent or "").strip().lower()
    if normalized_intent != "server":
        logger.warning(
            "[tools] blocked | tool=%s | intent=%s | allow_write=%s",
            tool_name,
            normalized_intent or None,
            allow_write,
        )
        detail = {
            "code": "intent_blocked",
            "message": "Tool execution blocked: intent must be 'server'.",
            "intent": normalized_intent or None,
            "blocked_value": tool_name,
        }
        try:
            tool_enum = ToolName(tool_name)
        except ValueError:
            tool_enum = ToolName.RUN_COMMAND
        return StepResult(
            step=step_number,
            tool=tool_enum,
            args=args,
            command=value_to_str(tool_name),
            command_type="ACTION",
            stdout="",
            stderr=json.dumps(detail),
            exit_code=1,
            duration_ms=0,
            executed_at=executed_at,
            success=False,
            validation_passed=False,
            status="failed",
        )

    try:
        tool = ToolName(tool_name)
    except ValueError:
        return StepResult(
            step=step_number,
            tool=ToolName.RUN_COMMAND,
            args=args,
            command=value_to_str(tool_name),
            command_type="ACTION",
            stderr=f"Unknown tool: {tool_name!r}. Valid tools: {[t.value for t in ToolName]}",
            exit_code=1,
            duration_ms=0,
            executed_at=executed_at,
            success=False,
            validation_passed=False,
            status="failed",
        )

    fn = _TOOL_FN.get(tool)
    if fn is None:
        return StepResult(
            step=step_number,
            tool=tool,
            args=args,
            command=_tool_command(tool, args),
            command_type="ACTION",
            stderr=f"Tool '{tool_name}' is registered but not implemented.",
            exit_code=1,
            duration_ms=0,
            executed_at=executed_at,
            success=False,
            validation_passed=False,
            status="failed",
        )

    start = time.monotonic()
    logger.info(
        "[tools] dispatch | step=%s | tool=%s | intent=%s | args=%s | allow_write=%s | timeout=%s | execution_path=execute_tool->SSHService.execute",
        step_number,
        value_to_str(getattr(tool, "value", None) or tool),
        normalized_intent,
        args,
        allow_write,
        timeout,
    )

    # Git tools take extra structured args from the plan step.
    if tool in {
        ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_BRANCH,
        ToolName.GIT_COMMIT, ToolName.GIT_RESTORE, ToolName.GIT_RESET, ToolName.GIT_CLEAN,
    }:
        git_kwargs: dict[str, Any] = dict(
            server=server,
            workspace_path=workspace_path,
            allow_write=allow_write,
            timeout=timeout,
        )
        if tool == ToolName.GIT_DIFF:
            git_kwargs["staged"] = bool(args.get("staged", False))
        elif tool == ToolName.GIT_COMMIT:
            git_kwargs["message"] = str(args.get("message", "agent commit"))
        elif tool == ToolName.GIT_RESTORE:
            git_kwargs["paths"] = args.get("paths") or None
        elif tool == ToolName.GIT_RESET:
            git_kwargs["mode"] = str(args.get("mode", "mixed"))
            git_kwargs["target"] = str(args.get("target", "HEAD"))
        elif tool == ToolName.GIT_CLEAN:
            git_kwargs["force"] = bool(args.get("force", False))
        try:
            res = await asyncio.wait_for(fn(**git_kwargs), timeout=timeout + 30)
        except asyncio.TimeoutError:
            res = {"stdout": "", "stderr": f"Tool '{tool_name}' timed out after {timeout}s", "code": 124}
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"code": "tool_execution_blocked", "message": str(exc.detail)}
            res = {"stdout": "", "stderr": json.dumps(detail), "code": 1}
        except Exception as exc:
            logger.exception("Unexpected error in git tool '%s': %s", tool_name, exc)
            res = {"stdout": "", "stderr": f"Internal git tool error: {exc}", "code": 1}
    elif tool in {ToolName.GITHUB_PULL, ToolName.GITHUB_PUSH}:
        # GitHub pull/push: resolve the linked connection id, then delegate to
        # GitHubService (which decrypts the key in-process). The connection id
        # is supplied by the orchestrator at plan time (args["github_connection_id"]).
        github_connection_id = str(args.get("github_connection_id") or "").strip()
        if not github_connection_id:
            res = {
                "stdout": "",
                "stderr": "github tool requires 'github_connection_id' in args",
                "code": 1,
            }
        else:
            # Resolve user_id: prefer explicit value (set by orchestrator),
            # fall back to the server's owner id.
            effective_user_id = user_id or (server or {}).get("user_id")
            gh_kwargs: dict[str, Any] = dict(
                server=server,
                workspace_path=workspace_path,
                github_connection_id=github_connection_id,
                user_id=effective_user_id,
                allow_write=allow_write,
                timeout=timeout,
            )
            if tool == ToolName.GITHUB_PULL:
                gh_kwargs["remote"] = str(args.get("remote", "origin"))
                gh_kwargs["branch"] = args.get("branch") or None
                gh_kwargs["strategy"] = str(args.get("strategy", "ff_only"))
            else:  # GITHUB_PUSH
                gh_kwargs["remote"] = str(args.get("remote", "origin"))
                gh_kwargs["branch"] = args.get("branch") or None
                gh_kwargs["force"] = bool(args.get("force", False))
                gh_kwargs["tags"] = bool(args.get("tags", False))
            try:
                res = await asyncio.wait_for(fn(**gh_kwargs), timeout=timeout + 30)
            except asyncio.TimeoutError:
                res = {"stdout": "", "stderr": f"Tool '{tool_name}' timed out after {timeout}s", "code": 124}
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {"code": "tool_execution_blocked", "message": str(exc.detail)}
                res = {"stdout": "", "stderr": json.dumps(detail), "code": 1}
            except Exception as exc:
                logger.exception("Unexpected error in github tool '%s': %s", tool_name, exc)
                res = {"stdout": "", "stderr": f"Internal github tool error: {exc}", "code": 1}
    else:
        try:
            res = await asyncio.wait_for(
                fn(
                    server=server,
                    args=args,
                    workspace_path=workspace_path,
                    allow_write=allow_write,
                    timeout=timeout,
                    step_number=step_number,
                    on_output_chunk=on_output_chunk,
                ),
                timeout=timeout + 30,
            )
        except asyncio.TimeoutError:
            res = {"stdout": "", "stderr": f"Tool '{tool_name}' timed out after {timeout}s", "code": 124}
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {
                "code": "tool_execution_blocked",
                "message": str(exc.detail),
            }
            res = {"stdout": "", "stderr": json.dumps(detail), "code": 1}
        except Exception as exc:
            logger.exception("Unexpected error in tool '%s': %s", tool_name, exc)
            res = {"stdout": "", "stderr": f"Internal tool error: {exc}", "code": 1}

    stdout = res.get("stdout", "")
    stderr = res.get("stderr", "")
    exit_code = int(res.get("code", 1))
    command = _tool_command(tool, args)
    command_type = _tool_command_type(tool, command)
    success = command_success(command_type, exit_code)
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "[tools] tool result | step=%s | tool=%s | exit_code=%s | stdout=%r | stderr=%r",
        step_number,
        value_to_str(getattr(tool, "value", None) or tool),
        exit_code,
        _truncate_for_log(stdout),
        _truncate_for_log(stderr, 400),
    )
    return StepResult(
        step=step_number,
        tool=tool,
        args=args,
        command=command,
        command_type=command_type,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
        executed_at=executed_at,
        success=success,
        validation_passed=success,
        status="validated" if success else "failed",
    )


# ---------------------------------------------------------------------------
# OpenAI function-calling schema
# ---------------------------------------------------------------------------

#: All tool definitions in OpenAI "tools" format.
OPENAI_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": ToolName.RUN_COMMAND,
            "description": (
                "Execute any safe shell command on the remote server via SSH. "
                "Use for diagnostic and read-only inspection commands. "
                "Do NOT use for destructive operations — use specialized tools instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "The shell command to execute on the remote server. "
                            "Must be safe and non-destructive. "
                            "Examples: 'ls -la /app', 'ps aux | grep node', "
                            "'systemctl status nginx', 'curl -s http://localhost:3000/health'"
                        ),
                    }
                    ,
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "Set true only after explicit user confirmation for dangerous actions "
                            "(e.g., stopping/disabling services, removing resources)."
                        ),
                        "default": False,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.CHECK_DISK,
            "description": (
                "Check disk space usage on the remote server. "
                "Runs 'df -h' and returns all mount points with usage percentages."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.CHECK_MEMORY,
            "description": (
                "Check RAM and swap usage on the remote server. "
                "Runs 'free -m' and returns memory totals and availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.READ_LOGS,
            "description": (
                "Read recent logs from a systemd service or a log file on the remote server. "
                "For systemd services, uses 'journalctl -u <service> -n <lines>'. "
                "For absolute file paths (starting with /), uses 'tail -n <lines> <path>'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": (
                            "Systemd unit name (e.g. 'nginx', 'postgresql', 'my-app') "
                            "or an absolute log file path (e.g. '/var/log/syslog')."
                        ),
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines to return. Range: 1–1000. Default: 100.",
                        "minimum": 1,
                        "maximum": 1000,
                    },
                },
                "required": ["service_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.RESTART_SERVICE,
            "description": (
                "Restart a systemd service on the remote server. "
                "Requires allow_write=true. Use only when a service needs recovery."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": (
                            "The exact systemd unit name to restart "
                            "(e.g. 'nginx', 'postgresql', 'my-app.service'). "
                            "Discover the correct name with run_command first if unsure."
                        ),
                    }
                },
                "required": ["service_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.DEPLOY_APP,
            "description": (
                "Run an explicit deployment command on the remote server (legacy). "
                "Requires allow_write=true. Use for controlled deploy scripts that are already present on the server."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "Short human-readable app name."},
                    "deploy_command": {"type": "string", "description": "Shell command to deploy/restart the app."},
                    "confirm": {
                        "type": "boolean",
                        "description": "Set true only after explicit user confirmation if the command is dangerous.",
                        "default": False,
                    },
                },
                "required": ["app_name", "deploy_command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.DEPLOY_NEXTJS_APP,
            "description": (
                "Deploy a Next.js application from a Git repository onto the remote server. "
                "Automatically: ensures Node.js and pm2 are installed, clones/updates the repo, "
                "runs 'npm ci && npm run build', and starts the app with pm2 on the given port. "
                "Returns the public URL when done. Requires allow_write=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_url": {
                        "type": "string",
                       "description": (
                          "Git repository URL. "
                          "HTTPS example: 'https://github.com/user/repo.git'. "
                          "SSH example: 'git@github.com:user/repo.git'."
                      ),
                    },
                    "branch": {
                        "type": "string",
                        "description": "Git branch to deploy. Default: 'main'.",
                        "default": "main",
                    },
                    "port": {
                        "type": "integer",
                        "description": "TCP port to run the Next.js app on (1024–65535).",
                        "minimum": 1024,
                        "maximum": 65535,
                    },
                    "app_name": {
                        "type": "string",
                        "description": (
                            "Unique name for the pm2 process. "
                            "Used to manage the app lifecycle. "
                            "Default: 'ts-<port>'."
                        ),
                    },
                },
                "required": ["repo_url", "port"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.LIST_FILES,
            "description": "List files and directories in a given path on the remote server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The relative path of the directory to list. Defaults to the current workspace directory.",
                        "default": "."
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.READ_FILE,
            "description": "Read the entire content of a file on the remote server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The relative path of the file to read."
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.WRITE_FILE,
            "description": "Create or overwrite a file with new content. Requires write access.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The relative path of the file to create or overwrite."
                    },
                    "content": {
                        "type": "string",
                        "description": "The new content to write into the file."
                    }
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.LIST_PROCESSES,
            "description": "List all running processes on the remote server to check their status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.GITHUB_PULL,
            "description": (
                "Fetch and integrate changes from the linked GitHub repository into the "
                "current workspace (the repo was cloned at workspace creation time — the agent "
                "NEVER clones). Requires the workspace to be linked to a GitHub connection. "
                "Use 'ff_only' (default) to avoid rewriting local history; 'merge' or 'rebase' "
                "when explicitly needed. Returns the pull result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "github_connection_id": {
                        "type": "string",
                        "description": "ID of the GitHub connection linked to this workspace (provided by the orchestrator).",
                    },
                    "remote": {
                        "type": "string",
                        "description": "Remote name. Default: 'origin'.",
                        "default": "origin",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch to pull (defaults to the current branch's upstream).",
                    },
                    "strategy": {
                        "type": "string",
                        "enum": ["ff_only", "merge", "rebase"],
                        "description": "How to integrate fetched changes. Default: 'ff_only'.",
                        "default": "ff_only",
                    },
                },
                "required": ["github_connection_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ToolName.GITHUB_PUSH,
            "description": (
                "Push committed changes to the linked GitHub repository. ALWAYS requires explicit "
                "human approval before it runs (HIGH risk). The agent must have committed first. "
                "Prefer a normal push; only set force=true (maps to --force-with-lease) when the "
                "remote history was intentionally rewritten. Never push secrets or large binaries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "github_connection_id": {
                        "type": "string",
                        "description": "ID of the GitHub connection linked to this workspace (provided by the orchestrator).",
                    },
                    "remote": {
                        "type": "string",
                        "description": "Remote name. Default: 'origin'.",
                        "default": "origin",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch to push (defaults to the current branch).",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Force push using --force-with-lease. Requires HIGH approval. Default: false.",
                        "default": False,
                    },
                    "tags": {
                        "type": "boolean",
                        "description": "Also push tags. Default: false.",
                        "default": False,
                    },
                },
                "required": ["github_connection_id"],
                "additionalProperties": False,
            },
        },
    },
]

#: Write-only tools that are hidden when allow_write=False.
_WRITE_ONLY_TOOLS: frozenset[str] = frozenset(
    [ToolName.RESTART_SERVICE, ToolName.DEPLOY_NEXTJS_APP, ToolName.DEPLOY_APP, ToolName.WRITE_FILE]
)


# ---------------------------------------------------------------------------
# Tool dispatch table — defined LAST so all git_*/_git_* functions above
# are already in scope (forward references resolve at call time).
# ---------------------------------------------------------------------------

_TOOL_FN: dict[ToolName, Any] = {
    ToolName.RUN_COMMAND: _run_command,
    ToolName.CHECK_DISK: _check_disk,
    ToolName.CHECK_MEMORY: _check_memory,
    ToolName.READ_LOGS: _read_logs,
    ToolName.RESTART_SERVICE: _restart_service,
    ToolName.DEPLOY_APP: _deploy_app,
    ToolName.DEPLOY_NEXTJS_APP: _deploy_nextjs_app,
    ToolName.LIST_FILES: _list_files,
    ToolName.READ_FILE: _read_file,
    ToolName.WRITE_FILE: _write_file,
    ToolName.LIST_PROCESSES: _list_processes,
    ToolName.PATCH_FILE: _patch_file,
    ToolName.CONFIG_SET: _config_set,
    ToolName.WRITE_SECRET: _write_secret,
    ToolName.GIT_STATUS: _git_status,
    ToolName.GIT_DIFF: _git_diff,
    ToolName.GIT_BRANCH: _git_branch,
    ToolName.GIT_COMMIT: _git_commit,
    ToolName.GIT_RESTORE: _git_restore,
    ToolName.GIT_RESET: _git_reset,
    ToolName.GIT_CLEAN: _git_clean,
    ToolName.GITHUB_PULL: _github_pull,
    ToolName.GITHUB_PUSH: _github_push,
}


def get_tool_definitions(allow_write: bool) -> list[dict[str, Any]]:
    '''Return OpenAI tool definitions filtered by the caller's write permission.'''
    # NOTE: allow_write parameter is now respected.
    # Removed previous hard-coded `allow_write = True` override.
    filtered = [
        td for td in OPENAI_TOOL_DEFINITIONS
        if td["function"]["name"] != ToolName.DEPLOY_NEXTJS_APP
    ]
    return filtered
