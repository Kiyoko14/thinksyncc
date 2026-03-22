from typing import Any

from fastapi import APIRouter, Depends

from core.security import get_current_user
from models.chat import (
    ChatMessageDualRequest,
    ChatMessageRequest,
    ChatResponse,
    ChatSendMessageDualResponse,
    ChatSendMessageResponse,
)
from services.ai_service import AIService
from services.chat_service import ChatService
from services.git_service import GitService
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

    ChatService.save_message(
        chat_id=chat["id"],
        role="user",
        content=payload.message,
    )

    ai_response = AIService.process_message(context=workspace, message=payload.message, context_type="workspace")

    ChatService.save_message(
        chat_id=chat["id"],
        role="assistant",
        content=ai_response,
    )

    return ChatSendMessageResponse(
        chat_id=chat["id"],
        workspace_id=workspace_id,
        response=ai_response,
    )


@router.get("/workspace/{workspace_id}", response_model=ChatResponse)
async def get_workspace_chat_v2(
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ChatResponse:
    workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=current_user["sub"])
    chat = ChatService.get_chat_by_workspace(workspace_id=workspace_id, user_id=current_user["sub"])
    if chat is None:
        chat = ChatService.create_chat_dual_context(
            user_id=current_user["sub"],
            workspace_id=workspace_id,
        )

    messages = ChatService.list_messages(chat_id=chat["id"])
    return ChatResponse(
        id=chat["id"],
        workspace_id=workspace["id"],
        user_id=current_user["sub"],
        created_at=chat["created_at"],
        messages=messages,
    )


@router.get("/repo/{git_repo_id}", response_model=ChatResponse)
async def get_repo_chat(
    git_repo_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ChatResponse:
    repo = GitService.get_repo_by_id(repo_id=git_repo_id, user_id=current_user["sub"])
    chat = ChatService.get_chat_by_git_repo(git_repo_id=git_repo_id, user_id=current_user["sub"])
    if chat is None:
        chat = ChatService.create_chat_dual_context(
            user_id=current_user["sub"],
            git_repo_id=git_repo_id,
        )

    messages = ChatService.list_messages(chat_id=chat["id"])
    return ChatResponse(
        id=chat["id"],
        workspace_id=repo.get("workspace_id", ""),
        user_id=current_user["sub"],
        created_at=chat["created_at"],
        messages=messages,
    )


@router.post("/message", response_model=ChatSendMessageDualResponse)
async def send_dual_context_message(
    payload: ChatMessageDualRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ChatSendMessageDualResponse:
    if payload.workspace_id:
        workspace = WorkspaceService.get_workspace_by_id(
            id=payload.workspace_id,
            user_id=current_user["sub"],
        )
        chat = ChatService.get_chat_by_workspace(
            workspace_id=payload.workspace_id,
            user_id=current_user["sub"],
        )
        if chat is None:
            chat = ChatService.create_chat_dual_context(
                user_id=current_user["sub"],
                workspace_id=payload.workspace_id,
            )

        ChatService.save_message(
            chat_id=chat["id"],
            role="user",
            content=payload.message,
        )

        ai_response = AIService.process_message(
            context=workspace,
            message=payload.message,
            context_type="workspace",
        )

        ChatService.save_message(
            chat_id=chat["id"],
            role="assistant",
            content=ai_response,
        )

        return ChatSendMessageDualResponse(
            chat_id=chat["id"],
            workspace_id=payload.workspace_id,
            response=ai_response,
        )
    else:
        repo = GitService.get_repo_by_id(
            repo_id=payload.git_repo_id,
            user_id=current_user["sub"],
        )
        chat = ChatService.get_chat_by_git_repo(
            git_repo_id=payload.git_repo_id,
            user_id=current_user["sub"],
        )
        if chat is None:
            chat = ChatService.create_chat_dual_context(
                user_id=current_user["sub"],
                git_repo_id=payload.git_repo_id,
            )

        ChatService.save_message(
            chat_id=chat["id"],
            role="user",
            content=payload.message,
        )

        ai_response = AIService.process_message(
            context=repo,
            message=payload.message,
            context_type="git_repo",
        )

        ChatService.save_message(
            chat_id=chat["id"],
            role="assistant",
            content=ai_response,
        )

        return ChatSendMessageDualResponse(
            chat_id=chat["id"],
            git_repo_id=payload.git_repo_id,
            response=ai_response,
        )
