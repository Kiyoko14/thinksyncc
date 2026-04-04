from typing import Any

from fastapi import APIRouter, Depends

from core.security import get_current_user
from models.chat import ChatMessageRequest, ChatResponse, ChatSendMessageResponse
from services.ai_service import AIService
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

    messages = ChatService.list_messages(chat_id=chat["id"])
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

    ChatService.save_message(chat_id=chat["id"], role="user", content=payload.message)
    ai_response = await AIService.process_message(workspace=workspace, message=payload.message)
    ChatService.save_message(chat_id=chat["id"], role="assistant", content=ai_response)

    return ChatSendMessageResponse(
        chat_id=chat["id"],
        workspace_id=workspace_id,
        response=ai_response,
    )
