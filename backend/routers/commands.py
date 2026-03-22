from typing import Any

from fastapi import APIRouter, Depends

from core.security import get_current_user
from models.message import CommandRequest, CommandResponse
from services.server_service import ServerService
from services.ssh_service import SSHService

router = APIRouter(prefix="/commands", tags=["commands"])


@router.post("/execute", response_model=CommandResponse)
async def execute_command(
    payload: CommandRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> CommandResponse:
    server = ServerService.get_server(
        server_id=payload.server_id,
        user_id=current_user["sub"],
    )
    return await SSHService.execute(server=server, command=payload.command)
