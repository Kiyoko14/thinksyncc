from typing import Any

from fastapi import APIRouter, Depends

from core.security import get_current_user
from core.value_coercion import value_to_str
from models.chat import ChatMessageRequest, ChatResponse, ChatSendMessageResponse
from services.chat_service import ChatService
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/{workspace_id}", response_model=ChatResponse)
async def get_workspace_chat(
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ChatResponse:
    workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=current_user["sub"])
    chat = ChatService.get_chat(workspace_id=workspace_id, user_id=current_user["sub"])
    if chat is None:
        chat = ChatService.create_chat(workspace_id=workspace_id, user_id=current_user["sub"])

    messages = ChatService.list_workspace_messages(workspace_id=workspace_id, user_id=current_user["sub"])
    return ChatResponse(
        id=chat["id"],
        workspace_id=workspace["id"],
        user_id=current_user["sub"],
        created_at=chat["created_at"],
        messages=messages,
    )


@router.post("/{workspace_id}/message", response_model=ChatSendMessageResponse)
async def send_workspace_message(
    workspace_id: str,
    payload: ChatMessageRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ChatSendMessageResponse:
    workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=current_user["sub"])
    chat = ChatService.get_chat(workspace_id=workspace_id, user_id=current_user["sub"])
    if chat is None:
        chat = ChatService.create_chat(workspace_id=workspace_id, user_id=current_user["sub"])

    stored_message = ChatService.save_workspace_message(
        workspace_id=workspace["id"],
        user_id=current_user["sub"],
        role=value_to_str(getattr(payload, "role", None)),
        content=payload.message,
    )

    return ChatSendMessageResponse(
        chat_id=chat["id"],
        workspace_id=workspace_id,
        response=stored_message.content,
        message=stored_message,
    )
