import shlex
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from core.security import get_current_user
from models.message import CommandRequest, CommandResponse
from services.server_service import ServerService
from services.ssh_service import SSHService
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/commands", tags=["commands"])


@router.post("/execute", response_model=CommandResponse)
async def execute_command(
    payload: CommandRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> CommandResponse:
    if not payload.workspace_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})
    workspace = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=current_user["sub"])
    if workspace.get("server_id") != payload.server_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "WORKSPACE_NOT_FOUND"})
    workspace_path = str(workspace.get("path") or "").strip()
    if not workspace_path:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"code": "WORKSPACE_PATH_MISSING"})

    server = ServerService.get_server(
        server_id=payload.server_id,
        user_id=current_user["sub"],
    )
    scoped = f"cd {shlex.quote(workspace_path)} && {payload.command}"
    return await SSHService.execute(server=server, command=scoped)
