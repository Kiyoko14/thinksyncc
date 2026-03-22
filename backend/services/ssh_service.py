from datetime import datetime, timezone
from typing import Any

import asyncssh
from fastapi import HTTPException, status

from core.config import get_settings
from models.message import CommandResponse


class SSHService:
    @staticmethod
    async def execute(server: dict[str, Any], command: str) -> CommandResponse:
        settings = get_settings()

        connect_kwargs: dict[str, Any] = {
            "host": server["host"],
            "port": server["ssh_port"],
            "username": server["ssh_user"],
            # TODO: replace with a known-hosts store before production use.
            "known_hosts": None,
            "connect_timeout": settings.SSH_TIMEOUT,
        }

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
                result = await conn.run(command, timeout=settings.SSH_COMMAND_TIMEOUT)
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
