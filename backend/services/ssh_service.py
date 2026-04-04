from datetime import datetime, timezone
import os
import subprocess
from typing import Any

import asyncssh
from fastapi import HTTPException, status

from core.config import get_settings
from models.message import CommandResponse


class SSHService:
    @staticmethod
    def ensure_known_host_entry(host: str, port: int) -> None:
        settings = get_settings()
        if not settings.SSH_STRICT_HOST_KEY_CHECKING:
            return

        known_hosts_path = os.path.expanduser(settings.SSH_KNOWN_HOSTS)
        known_hosts_dir = os.path.dirname(known_hosts_path)

        os.makedirs(known_hosts_dir, mode=0o700, exist_ok=True)
        if not os.path.exists(known_hosts_path):
            with open(known_hosts_path, "a", encoding="utf-8"):
                pass
            os.chmod(known_hosts_path, 0o600)

        target = host if port == 22 else f"[{host}]:{port}"
        lookup = subprocess.run(
            ["ssh-keygen", "-F", target, "-f", known_hosts_path],
            capture_output=True,
            text=True,
            timeout=settings.SSH_TIMEOUT,
            check=False,
        )
        if lookup.returncode == 0:
            return

        try:
            keyscan = subprocess.run(
                [
                    "ssh-keyscan",
                    "-T",
                    str(settings.SSH_TIMEOUT),
                    "-p",
                    str(port),
                    "-H",
                    host,
                ],
                capture_output=True,
                text=True,
                timeout=settings.SSH_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unable to reach SSH host while fetching host key",
            )
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to run ssh-keyscan: {exc}",
            )

        scanned = keyscan.stdout.strip()
        if keyscan.returncode != 0 or not scanned:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to fetch SSH host key for the provided server",
            )

        with open(known_hosts_path, "a", encoding="utf-8") as fh:
            fh.write(scanned)
            fh.write("\n")

    @staticmethod
    async def validate_server_connection(
        host: str,
        port: int,
        username: str,
        auth_method: str,
        ssh_password: str | None = None,
        ssh_key: str | None = None,
    ) -> None:
        settings = get_settings()

        if settings.SSH_STRICT_HOST_KEY_CHECKING:
            SSHService.ensure_known_host_entry(host=host, port=port)

        connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "username": username,
            "connect_timeout": settings.SSH_TIMEOUT,
        }

        if settings.SSH_STRICT_HOST_KEY_CHECKING:
            connect_kwargs["known_hosts"] = os.path.expanduser(settings.SSH_KNOWN_HOSTS)
        else:
            connect_kwargs["known_hosts"] = None

        if auth_method == "key":
            if not ssh_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="SSH key is required for key authentication",
                )
            connect_kwargs["client_keys"] = [asyncssh.import_private_key(ssh_key)]
        elif auth_method == "password":
            if not ssh_password:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="SSH password is required for password authentication",
                )
            connect_kwargs["password"] = ssh_password
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported auth method: {auth_method}",
            )

        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                probe = await conn.run("echo thinksync-ssh-check", timeout=settings.SSH_COMMAND_TIMEOUT)
                if probe.exit_status not in (0, None):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="SSH server is reachable but command execution failed",
                    )
        except asyncssh.PermissionDenied:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="SSH authentication failed — invalid username/password or key",
            )
        except (OSError, TimeoutError, asyncssh.Error) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unable to connect to SSH server: {exc}",
            )

    @staticmethod
    async def execute(
        server: dict[str, Any],
        command: str,
        command_timeout: int | None = None,
    ) -> CommandResponse:
        settings = get_settings()

        connect_kwargs: dict[str, Any] = {
            "host": server["host"],
            "port": server["ssh_port"],
            "username": server["ssh_user"],
            "connect_timeout": settings.SSH_TIMEOUT,
        }

        if settings.SSH_STRICT_HOST_KEY_CHECKING:
            known_hosts_path = os.path.expanduser(settings.SSH_KNOWN_HOSTS)
            if not os.path.exists(known_hosts_path):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="SSH known_hosts file is missing on backend server",
                )
            connect_kwargs["known_hosts"] = known_hosts_path
        else:
            connect_kwargs["known_hosts"] = None

        if server["ssh_auth_method"] == "key":
            ssh_key = server.get("ssh_key")
            if not ssh_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="SSH key is missing for this server",
                )
            connect_kwargs["client_keys"] = [asyncssh.import_private_key(ssh_key)]
        elif server["ssh_auth_method"] == "password":
            ssh_password = server.get("ssh_password")
            if not ssh_password:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="SSH password is missing for this server",
                )
            connect_kwargs["password"] = ssh_password
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported auth method: {server['ssh_auth_method']}",
            )

        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                effective_timeout = command_timeout or settings.SSH_COMMAND_TIMEOUT
                result = await conn.run(command, timeout=effective_timeout)
                return CommandResponse(
                    server_id=str(server["id"]),
                    command=command,
                    output=result.stdout or result.stderr or "",
                    exit_code=result.exit_status if result.exit_status is not None else 0,
                    executed_at=datetime.now(timezone.utc),
                )
        except asyncssh.DisconnectError as exc:
            if "permission denied" in str(exc).lower():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="SSH authentication failed — check credentials",
                )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"SSH connection lost: {exc}",
            )
        except asyncssh.PermissionDenied:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="SSH authentication failed — check credentials",
            )
        except asyncssh.Error as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"SSH error: {exc}",
            )
        except TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="SSH command timed out",
            )
