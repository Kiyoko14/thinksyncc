"""
ThinkSync Tool Registry.

All tools execute REAL operations on remote servers via SSH.
Nothing is simulated or faked.

Every tool function signature:
    async def <name>(*, server, args, allow_write, timeout) -> tuple[str, str, int]
    returns (stdout, stderr, exit_code)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from models.agent import StepResult, ToolName
from services.ssh_service import SSHService

logger = logging.getLogger(__name__)

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


def _validate_command(command: str, allow_write: bool) -> None:
    if _is_dangerous(command):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Rejected dangerous command: {command!r}",
        )
    if not allow_write:
        lowered = command.strip().lower()
        if not any(lowered.startswith(p) for p in _READ_ONLY_PREFIXES):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Command not allowed in read-only mode: {command!r}. "
                    "Set allow_write=true or use a safe read-only command."
                ),
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


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _run_command(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    command: str = args.get("command", "").strip()
    if not command:
        return "", "run_command: 'command' argument is required", 1
    _validate_command(command, allow_write=allow_write)
    resp = await SSHService.execute(server=server, command=command, command_timeout=timeout)
    if resp.exit_code == 0:
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


async def _check_disk(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    resp = await SSHService.execute(server=server, command="df -h", command_timeout=timeout)
    return resp.output, "", resp.exit_code


async def _check_memory(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    resp = await SSHService.execute(server=server, command="free -m", command_timeout=timeout)
    return resp.output, "", resp.exit_code


async def _read_logs(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    service_name: str = args.get("service_name", "").strip()
    lines: int = max(1, min(int(args.get("lines", 100)), 1000))

    if not service_name:
        return "", "read_logs: 'service_name' argument is required", 1

    if service_name.startswith("/"):
        # Absolute path — read file directly
        if _is_dangerous(f"tail -n {lines} {service_name}"):
            return "", f"Rejected dangerous log path: {service_name!r}", 1
        command = f"tail -n {lines} {service_name}"
    else:
        _validate_service_name(service_name)
        command = f"journalctl -u {service_name} -n {lines} --no-pager"

    resp = await SSHService.execute(server=server, command=command, command_timeout=timeout)
    if resp.exit_code == 0:
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


async def _restart_service(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    if not allow_write:
        return "", "restart_service requires allow_write=true.", 1

    service_name: str = args.get("service_name", "").strip()
    if not service_name:
        return "", "restart_service: 'service_name' argument is required", 1

    _validate_service_name(service_name)
    command = f"systemctl restart {service_name}"
    resp = await SSHService.execute(server=server, command=command, command_timeout=timeout)
    if resp.exit_code == 0:
        return f"Service '{service_name}' restarted successfully.", "", 0
    return "", resp.output, resp.exit_code


async def _deploy_app(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    """Legacy deploy_app tool — runs a deploy command via SSH."""
    if not allow_write:
        return "", "deploy_app requires allow_write=true.", 1

    app_name: str = args.get("app_name", "").strip()
    deploy_command: str = args.get("deploy_command", "").strip()

    if not app_name or not deploy_command:
        return "", "deploy_app: 'app_name' and 'deploy_command' are required", 1

    _validate_command(deploy_command, allow_write=True)
    resp = await SSHService.execute(server=server, command=deploy_command, command_timeout=timeout)
    if resp.exit_code == 0:
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


async def _deploy_nextjs_app(
    *,
    server: dict[str, Any],
    args: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    """
    Full Next.js deployment pipeline on the remote server:
      1. Ensure Node.js (LTS) and pm2 are installed
      2. Clone or update the repository
      3. npm ci / npm install
      4. npm run build  (NODE_ENV=production)
      5. Start / restart via pm2 on the specified port
      6. Return the public URL
    """
    if not allow_write:
        return "", "deploy_nextjs_app requires allow_write=true.", 1

    repo_url: str = args.get("repo_url", "").strip()
    branch: str = args.get("branch", "main").strip()
    port: int = int(args.get("port", 3000))
    raw_name = args.get("app_name", f"ts-{port}").strip()
    app_name = _sanitize_app_name(raw_name)

    if not repo_url:
        return "", "deploy_nextjs_app: 'repo_url' is required", 1

    _validate_repo_url(repo_url)
    _validate_port(port)

    if not re.match(r"^[a-zA-Z0-9_.\\-/]+$", branch):
        return "", f"Invalid branch name: {branch!r}", 1

    # Build a self-contained bash script.
    # We base64-encode it so that special characters in any of the
    # user-supplied values (repo_url, branch, app_name) cannot break
    # the outer shell invocation.
    deploy_script = (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "\n"
        f"REPO_URL='{repo_url}'\n"
        f"BRANCH='{branch}'\n"
        f"PORT={port}\n"
        f"APP_NAME='{app_name}'\n"
        "APPS_DIR='/opt/thinksync-apps'\n"
        "APP_DIR=\"$APPS_DIR/$APP_NAME\"\n"
        "\n"
        "echo '=== ThinkSync: Next.js Deployment ==='\n"
        "echo \"Repo   : $REPO_URL\"\n"
        "echo \"Branch : $BRANCH\"\n"
        "echo \"Port   : $PORT\"\n"
        "echo \"Name   : $APP_NAME\"\n"
        "\n"
        "# Ensure apps directory exists\n"
        "mkdir -p \"$APPS_DIR\"\n"
        "\n"
        "# --- Install Node.js (LTS) if missing ---\n"
        "if ! command -v node >/dev/null 2>&1; then\n"
        "    echo '--- Installing Node.js LTS ---'\n"
        "    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -\n"
        "    apt-get install -y nodejs\n"
        "fi\n"
        "echo \"Node: $(node --version)  npm: $(npm --version)\"\n"
        "\n"
        "# --- Install pm2 globally if missing ---\n"
        "if ! command -v pm2 >/dev/null 2>&1; then\n"
        "    echo '--- Installing pm2 ---'\n"
        "    npm install -g pm2\n"
        "fi\n"
        "\n"
        "# --- Stop existing pm2 process (ignore errors) ---\n"
        "pm2 stop \"$APP_NAME\" 2>/dev/null || true\n"
        "pm2 delete \"$APP_NAME\" 2>/dev/null || true\n"
        "\n"
        "# --- Clone or pull latest code ---\n"
        "if [ -d \"$APP_DIR/.git\" ]; then\n"
        "    echo '--- Updating existing repository ---'\n"
        "    cd \"$APP_DIR\"\n"
        "    git fetch origin\n"
        "    git checkout \"$BRANCH\"\n"
        "    git reset --hard \"origin/$BRANCH\"\n"
        "else\n"
        "    echo '--- Cloning repository ---'\n"
        "    rm -rf \"$APP_DIR\"\n"
        "    git clone --branch \"$BRANCH\" --depth 1 \"$REPO_URL\" \"$APP_DIR\"\n"
        "    cd \"$APP_DIR\"\n"
        "fi\n"
        "\n"
        "# --- Install dependencies ---\n"
        "echo '--- Installing dependencies ---'\n"
        "if [ -f package-lock.json ]; then\n"
        "    npm ci\n"
        "else\n"
        "    npm install\n"
        "fi\n"
        "\n"
        "# --- Build production bundle ---\n"
        "echo '--- Building Next.js app ---'\n"
        "NODE_ENV=production npm run build\n"
        "\n"
        "# --- Start app with pm2 ---\n"
        "echo '--- Starting app with pm2 ---'\n"
        "PORT=$PORT pm2 start npm --name \"$APP_NAME\" -- start\n"
        "pm2 save\n"
        "\n"
        "# --- Report result ---\n"
        "SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || hostname)\n"
        "echo ''\n"
        "echo '=== Deployment successful ==='\n"
        "echo \"App URL : http://$SERVER_IP:$PORT\"\n"
        "echo \"pm2 name: $APP_NAME\"\n"
        "pm2 show \"$APP_NAME\" 2>/dev/null || true\n"
    )

    # Base64-encode the script to avoid quoting issues in the outer shell
    script_b64 = base64.b64encode(deploy_script.encode()).decode()
    # Give deployment at least 10 minutes regardless of caller timeout
    deploy_timeout = max(timeout, 600)
    command = f"echo '{script_b64}' | base64 -d | bash"

    resp = await SSHService.execute(
        server=server,
        command=command,
        command_timeout=deploy_timeout,
    )
    if resp.exit_code == 0:
        return resp.output, "", 0
    return "", resp.output, resp.exit_code


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
    allow_write: bool,
    timeout: int,
    step_number: int = 0,
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
    try:
        stdout, stderr, exit_code = await asyncio.wait_for(
            fn(server=server, args=args, allow_write=allow_write, timeout=timeout),
            timeout=timeout + 30,
        )
    except asyncio.TimeoutError:
        stdout, stderr, exit_code = "", f"Tool '{tool_name}' timed out after {timeout}s", 124
    except HTTPException as exc:
        stdout, stderr, exit_code = "", str(exc.detail), 1
    except Exception as exc:
        logger.exception("Unexpected error in tool '%s': %s", tool_name, exc)
        stdout, stderr, exit_code = "", f"Internal tool error: {exc}", 1

    duration_ms = int((time.monotonic() - start) * 1000)
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
    if allow_write:
        return OPENAI_TOOL_DEFINITIONS
    return [
        td for td in OPENAI_TOOL_DEFINITIONS
        if td["function"]["name"] not in _WRITE_ONLY_TOOLS
    ]
