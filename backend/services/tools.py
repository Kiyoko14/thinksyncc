"""
ThinkSync Tool Registry.

All tools execute REAL operations on remote servers via SSH.
Nothing is simulated or faked.

Every tool function signature:
    async def <name>(*, server, args, workspace_path, allow_write, timeout) -> tuple[str, str, int]
    returns (stdout, stderr, exit_code)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from models.agent import StepResult, ToolName
from services.ssh_service import SSHService

logger = logging.getLogger(__name__)

OutputChunkCallback = Callable[[int, str, str, str], Awaitable[None] | None]

# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

_BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b", flags=re.IGNORECASE),
    re.compile(r"\bmkfs\b", flags=re.IGNORECASE),
    re.compile(r"\bdd\s+if=", flags=re.IGNORECASE),
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
    "/home/root/workspaces",
    "/root/thinksync",
    "/tmp",
)

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


def _validate_command(command: str, allow_write: bool) -> None:
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

    # Git clone restriction
    if "git clone" in lowered and not allow_write:
        raise _guard_error(
            code="git_clone_blocked",
            message="git clone requires explicit write permission.",
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

    if not allow_write:
        if not any(lowered.startswith(p) for p in _READ_ONLY_PREFIXES):
            raise _guard_error(
                code="read_only_command_blocked",
                message="Command is not allowed in read-only mode. Set allow_write=true or use a safe diagnostic command.",
                blocked_value=command,
            )


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
    return f"{value[:limit]}...<truncated>"


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
    if not cleaned.startswith("/home/root/workspaces/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_workspace_path", "message": "Workspace path must be under /home/root/workspaces"},
        )
    return f"cd {shlex.quote(cleaned)} && {command}"

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _run_command(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> tuple[str, str, int]:
    command: str = args.get("command", "").strip()
    if not command:
        return "", "run_command: 'command' argument is required", 1
    _validate_command(command, allow_write=allow_write)
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
    if resp.exit_code == 0:
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


async def _check_disk(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> tuple[str, str, int]:
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
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


async def _check_memory(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> tuple[str, str, int]:
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
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


async def _read_logs(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> tuple[str, str, int]:
    service_name: str = args.get("service_name", "").strip()
    lines: int = max(1, min(int(args.get("lines", 100)), 1000))

    if not service_name:
        return "", "read_logs: 'service_name' argument is required", 1

    if service_name.startswith("/"):
        # Absolute path — read file directly
        candidate = f"tail -n {lines} {service_name}"
        try:
            _validate_command(candidate, allow_write=False)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            return "", json.dumps(detail), 1
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
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


async def _restart_service(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> tuple[str, str, int]:
    if not allow_write:
        return "", "restart_service requires allow_write=true.", 1

    service_name: str = args.get("service_name", "").strip()
    if not service_name:
        return "", "restart_service: 'service_name' argument is required", 1

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
        return f"Service '{service_name}' restarted successfully.", "", 0
    return "", resp.output, resp.exit_code


async def _deploy_app(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> tuple[str, str, int]:
    """Legacy deploy_app tool — runs a deploy command via SSH."""
    if not allow_write:
        return "", "deploy_app requires allow_write=true.", 1

    app_name: str = args.get("app_name", "").strip()
    deploy_command: str = args.get("deploy_command", "").strip()

    if not app_name or not deploy_command:
        return "", "deploy_app: 'app_name' and 'deploy_command' are required", 1

    _validate_command(deploy_command, allow_write=True)
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
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


async def _deploy_nextjs_app(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int,
    on_output_chunk: OutputChunkCallback | None = None,
) -> tuple[str, str, int]:
    return "", json.dumps({
        "code": "tool_disabled",
        "message": "deploy_nextjs_app is disabled. Use deploy_app with an explicit remote-safe deploy command.",
        "blocked_value": ToolName.DEPLOY_NEXTJS_APP.value,
    }), 1


# ---------------------------------------------------------------------------
# Tool dispatch table
# ---------------------------------------------------------------------------

_TOOL_FN: dict[ToolName, Any] = {
    ToolName.RUN_COMMAND: _run_command,
    ToolName.CHECK_DISK: _check_disk,
    ToolName.CHECK_MEMORY: _check_memory,
    ToolName.READ_LOGS: _read_logs,
    ToolName.RESTART_SERVICE: _restart_service,
    ToolName.DEPLOY_APP: _deploy_app,
    ToolName.DEPLOY_NEXTJS_APP: _deploy_nextjs_app,
}


async def execute_tool(
    *,
    tool_name: str,
    args: dict[str, Any],
    server: dict[str, Any],
    workspace_path: str,
    allow_write: bool,
    timeout: int,
    step_number: int = 0,
    on_output_chunk: OutputChunkCallback | None = None,
) -> StepResult:
    """
    Dispatch a named tool call to its implementation and return a StepResult.
    All execution is real — no simulation, no faking.
    """
    executed_at = datetime.now(timezone.utc)

    try:
        tool = ToolName(tool_name)
    except ValueError:
        return StepResult(
            step=step_number,
            tool=ToolName.RUN_COMMAND,
            args=args,
            stderr=f"Unknown tool: {tool_name!r}. Valid tools: {[t.value for t in ToolName]}",
            exit_code=1,
            duration_ms=0,
            executed_at=executed_at,
            success=False,
        )

    fn = _TOOL_FN.get(tool)
    if fn is None:
        return StepResult(
            step=step_number,
            tool=tool,
            args=args,
            stderr=f"Tool '{tool_name}' is registered but not implemented.",
            exit_code=1,
            duration_ms=0,
            executed_at=executed_at,
            success=False,
        )

    start = time.monotonic()
    logger.info(
        "[tools] dispatch | step=%s | tool=%s | args=%s | allow_write=%s | timeout=%s | execution_path=execute_tool->SSHService.execute",
        step_number,
        tool.value,
        args,
        allow_write,
        timeout,
    )
    try:
        stdout, stderr, exit_code = await asyncio.wait_for(
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
        stdout, stderr, exit_code = "", f"Tool '{tool_name}' timed out after {timeout}s", 124
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {
            "code": "tool_execution_blocked",
            "message": str(exc.detail),
        }
        stdout, stderr, exit_code = "", json.dumps(detail), 1
    except Exception as exc:
        logger.exception("Unexpected error in tool '%s': %s", tool_name, exc)
        stdout, stderr, exit_code = "", f"Internal tool error: {exc}", 1

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "[tools] tool result | step=%s | tool=%s | exit_code=%s | stdout=%r | stderr=%r",
        step_number,
        tool.value,
        exit_code,
        _truncate_for_log(stdout),
        _truncate_for_log(stderr, 400),
    )
    return StepResult(
        step=step_number,
        tool=tool,
        args=args,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
        executed_at=executed_at,
        success=(exit_code == 0),
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
]

#: Write-only tools that are hidden when allow_write=False.
_WRITE_ONLY_TOOLS: frozenset[str] = frozenset(
    [ToolName.RESTART_SERVICE, ToolName.DEPLOY_NEXTJS_APP, ToolName.DEPLOY_APP]
)


def get_tool_definitions(allow_write: bool) -> list[dict[str, Any]]:
    """Return OpenAI tool definitions filtered by the caller's write permission."""
    filtered = [
        td for td in OPENAI_TOOL_DEFINITIONS
        if td["function"]["name"] != ToolName.DEPLOY_NEXTJS_APP
    ]
    if allow_write:
        return filtered
    return [
        td for td in filtered
        if td["function"]["name"] not in _WRITE_ONLY_TOOLS
    ]
