from __future__ import annotations

import base64
import os
import re
import shlex
from typing import Any

from fastapi import HTTPException, status

from core.config import get_settings
from services.ssh_service import SSHService


class DeployService:
    @staticmethod
    def _base_domain() -> str:
        settings = get_settings()
        value = (getattr(settings, "WORKSPACE_BASE_DOMAIN", None) or "").strip()
        if not value:
            value = os.getenv("THINKSYNC_WORKSPACE_BASE_DOMAIN", "").strip()
        return value or "thinksync.art"

    @staticmethod
    def _sites_available() -> str:
        settings = get_settings()
        value = (getattr(settings, "NGINX_SITES_AVAILABLE", None) or "").strip()
        if not value:
            value = os.getenv("THINKSYNC_NGINX_SITES_AVAILABLE", "").strip()
        return value or "/etc/nginx/sites-available"

    @staticmethod
    def _sites_enabled() -> str:
        settings = get_settings()
        value = (getattr(settings, "NGINX_SITES_ENABLED", None) or "").strip()
        if not value:
            value = os.getenv("THINKSYNC_NGINX_SITES_ENABLED", "").strip()
        return value or "/etc/nginx/sites-enabled"

    @staticmethod
    def _require_slug(slug: str) -> str:
        cleaned = (slug or "").strip().lower()
        if not cleaned or not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", cleaned):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_workspace", "message": "Invalid workspace slug"},
            )
        return cleaned

    @staticmethod
    def _require_port(port: int) -> int:
        try:
            value = int(port)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_port", "message": "Invalid port"},
            )
        if value < 1 or value > 65535:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_port", "message": "Port must be 1–65535"},
            )
        return value

    @staticmethod
    def _render_nginx_proxy(*, server_name: str, port: int) -> str:
        # Supports WebSockets and forwards common headers.
        return (
            "server {\n"
            "    listen 80;\n"
            f"    server_name {server_name};\n"
            "\n"
            "    location / {\n"
            f"        proxy_pass http://127.0.0.1:{port};\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            "        proxy_set_header Connection $connection_upgrade;\n"
            "    }\n"
            "}\n"
        )

    @staticmethod
    async def _write_remote_file(*, server: dict[str, Any], path: str, content: str, timeout: int) -> tuple[str, str, int]:
        encoded = base64.b64encode((content or "").encode("utf-8")).decode("ascii")
        if len(encoded) > 250_000:
            return "", "nginx config too large", 1
        py = (
            "import base64,os\n"
            f"p={path!r}\n"
            "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            f"data=base64.b64decode({encoded!r})\n"
            "with open(p,'wb') as f:\n"
            "  f.write(data)\n"
        )
        resp = await SSHService.execute(server=server, command=f"python3 -c {shlex.quote(py)}", command_timeout=timeout)
        return resp.stdout, resp.stderr, resp.exit_code

    @staticmethod
    async def expose_workspace_subdomain(
        *,
        server: dict[str, Any],
        workspace_slug: str,
        domain: str | None = None,
        port: int,
        allow_write: bool | None,
        timeout: int,
    ) -> dict[str, Any]:
        allow_write = True

        slug = DeployService._require_slug(workspace_slug)
        port_value = DeployService._require_port(port)

        base_domain = DeployService._base_domain()
        cleaned_domain = (domain or "").strip().lower()
        server_name = cleaned_domain or f"{slug}.{base_domain}"
        sites_available = DeployService._sites_available().rstrip("/")
        sites_enabled = DeployService._sites_enabled().rstrip("/")
        # Backward-compatible filename selection:
        # Prefer `{workspace}` (no extension), but never create duplicates:
        # - If an enabled entry already exists (either variant), reuse it.
        # - Else if an available config exists (either variant), reuse it.
        # - Else create `{workspace}`.
        filename_primary = slug
        filename_legacy = f"{slug}.conf"
        enabled_primary = f"{sites_enabled}/{filename_primary}"
        enabled_legacy = f"{sites_enabled}/{filename_legacy}"
        available_primary = f"{sites_available}/{filename_primary}"
        available_legacy = f"{sites_available}/{filename_legacy}"

        enabled_primary_exists = await SSHService.execute(server=server, command=f"test -e {shlex.quote(enabled_primary)}", command_timeout=timeout)
        enabled_legacy_exists = await SSHService.execute(server=server, command=f"test -e {shlex.quote(enabled_legacy)}", command_timeout=timeout)

        chosen_filename = filename_primary
        if enabled_primary_exists.exit_code == 0:
            chosen_filename = filename_primary
        elif enabled_legacy_exists.exit_code == 0:
            chosen_filename = filename_legacy
        else:
            avail_primary_exists = await SSHService.execute(server=server, command=f"test -f {shlex.quote(available_primary)}", command_timeout=timeout)
            if avail_primary_exists.exit_code == 0:
                chosen_filename = filename_primary
            else:
                avail_legacy_exists = await SSHService.execute(server=server, command=f"test -f {shlex.quote(available_legacy)}", command_timeout=timeout)
                if avail_legacy_exists.exit_code == 0:
                    chosen_filename = filename_legacy

        available_path = f"{sites_available}/{chosen_filename}"
        enabled_path = f"{sites_enabled}/{chosen_filename}"

        config = DeployService._render_nginx_proxy(server_name=server_name, port=port_value)
        # NGINX SAFETY: never overwrite an existing config.
        config_exists = False
        exists_resp = await SSHService.execute(server=server, command=f"test -f {shlex.quote(available_path)}", command_timeout=timeout)
        config_exists = exists_resp.exit_code == 0

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        if not config_exists:
            stdout, stderr, exit_code = await DeployService._write_remote_file(
                server=server,
                path=available_path,
                content=config,
                timeout=timeout,
            )
            if stdout:
                stdout_parts.append(stdout)
            if stderr:
                stderr_parts.append(stderr)
            if exit_code != 0:
                return {
                    "success": False,
                    "stdout": "".join(stdout_parts).strip(),
                    "stderr": "".join(stderr_parts).strip(),
                    "exit_code": exit_code,
                    "url": None,
                    "reused_domain": False,
                }

        enabled_exists = False
        enabled_resp = await SSHService.execute(server=server, command=f"test -e {shlex.quote(enabled_path)}", command_timeout=timeout)
        enabled_exists = enabled_resp.exit_code == 0
        if not enabled_exists:
            link_cmd = f"ln -s {shlex.quote(available_path)} {shlex.quote(enabled_path)}"
            resp = await SSHService.execute(server=server, command=link_cmd, command_timeout=timeout)
            if resp.stdout:
                stdout_parts.append(resp.stdout)
            if resp.stderr:
                stderr_parts.append(resp.stderr)
            if resp.exit_code != 0:
                return {
                    "success": False,
                    "stdout": "".join(stdout_parts).strip(),
                    "stderr": "".join(stderr_parts).strip(),
                    "exit_code": resp.exit_code,
                    "url": None,
                    "reused_domain": bool(config_exists),
                }

        reload_needed = (not config_exists) or (not enabled_exists)
        reload_code = 0
        if reload_needed:
            # Validate nginx config then reload.
            test = await SSHService.execute(server=server, command="nginx -t", command_timeout=timeout)
            if test.stdout:
                stdout_parts.append(test.stdout)
            if test.stderr:
                stderr_parts.append(test.stderr)
            if test.exit_code != 0:
                return {
                    "success": False,
                    "stdout": "".join(stdout_parts).strip(),
                    "stderr": "".join(stderr_parts).strip(),
                    "exit_code": test.exit_code,
                    "url": None,
                    "reused_domain": bool(config_exists),
                }

            reload_cmds = [
                "systemctl reload nginx",
                "service nginx reload",
                "nginx -s reload",
            ]
            reload_code = 1
            for cmd in reload_cmds:
                r = await SSHService.execute(server=server, command=cmd, command_timeout=timeout)
                if r.stdout:
                    stdout_parts.append(r.stdout)
                if r.stderr:
                    stderr_parts.append(r.stderr)
                reload_code = r.exit_code
                if r.exit_code == 0:
                    break

        return {
            "success": (reload_code == 0),
            "stdout": "".join(stdout_parts).strip(),
            "stderr": "".join(stderr_parts).strip(),
            "exit_code": int(reload_code),
            "url": f"https://{server_name}",
            "reused_domain": bool(config_exists),
        }
