from typing import Any

from fastapi import APIRouter, Depends, status

from core.security import get_current_user
from models.server import ServerCreate, ServerResponse
from services.server_service import ServerService

router = APIRouter(prefix="/servers", tags=["servers"])


@router.get("/", response_model=list[ServerResponse])
async def list_servers(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[ServerResponse]:
    return ServerService.list_servers(user_id=current_user["sub"])


@router.post("/", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def add_server(
    payload: ServerCreate,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ServerResponse:
    return await ServerService.create_server(user_id=current_user["sub"], data=payload)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> None:
    ServerService.delete_server(server_id=server_id, user_id=current_user["sub"])
