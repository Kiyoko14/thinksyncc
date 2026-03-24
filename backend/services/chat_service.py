from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.database import get_supabase
from models.chat import ChatMessageRole, StoredMessageResponse
from services.workspace_service import WorkspaceService


class ChatService:
    @staticmethod
    def _api_error_code(exc: APIError) -> str:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return code.upper()

        first_arg = exc.args[0] if exc.args else None
        if isinstance(first_arg, dict):
            raw_code = first_arg.get("code")
            if isinstance(raw_code, str):
                return raw_code.upper()

        return ""

    @staticmethod
    def _validate_uuid(value: str, field_name: str) -> None:
        try:
            UUID(value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {field_name} format",
            )

    @staticmethod
    def get_chat(workspace_id: str, user_id: str) -> dict[str, Any] | None:
        ChatService._validate_uuid(workspace_id, "workspace_id")
        ChatService._validate_uuid(user_id, "user_id")

        WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

        supabase = get_supabase()
        result = (
            supabase.table("chats")
            .select("*")
            .eq("workspace_id", workspace_id)
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if not result or not result.data:
            return None

        return result.data[0]

    @staticmethod
    def create_chat(workspace_id: str, user_id: str) -> dict[str, Any]:
        ChatService._validate_uuid(workspace_id, "workspace_id")
        ChatService._validate_uuid(user_id, "user_id")

        workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

        existing = ChatService.get_chat(workspace_id=workspace_id, user_id=user_id)
        if existing:
            return existing

        supabase = get_supabase()
        payload = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = supabase.table("chats").insert(payload).execute()
        except APIError as exc:
            code = ChatService._api_error_code(exc)
            if code == "23502":
                # Backward compatibility: legacy schema requires server_id and name.
                legacy_payload = {
                    **payload,
                    "server_id": workspace["server_id"],
                    "name": f"Workspace: {workspace['name']}",
                }
                try:
                    result = supabase.table("chats").insert(legacy_payload).execute()
                except APIError as legacy_exc:
                    legacy_code = ChatService._api_error_code(legacy_exc)
                    if legacy_code in {"23503", "42501"}:
                        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
                    if legacy_code == "22P02":
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request data")
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create chat")
            elif code in {"23503", "42501"}:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
            elif code == "22P02":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request data")
            else:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create chat")

        if not result or not result.data:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create chat")

        return result.data[0]

    @staticmethod
    def save_message(chat_id: str, role: str, content: str) -> StoredMessageResponse:
        ChatService._validate_uuid(chat_id, "chat_id")

        if role not in {ChatMessageRole.USER.value, ChatMessageRole.ASSISTANT.value, ChatMessageRole.SYSTEM.value}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid message role")

        cleaned_content = content.strip()
        if not cleaned_content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message content is required")

        supabase = get_supabase()
        try:
            result = (
                supabase.table("messages")
                .insert({
                    "chat_id": chat_id,
                    "role": role,
                    "content": cleaned_content,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                .execute()
            )
        except APIError as exc:
            code = ChatService._api_error_code(exc)
            if code in {"23503", "42501"}:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
            if code == "22P02":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request data")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save message")

        if not result or not result.data:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save message")

        return StoredMessageResponse(**result.data[0])

    @staticmethod
    def list_messages(chat_id: str) -> list[StoredMessageResponse]:
        ChatService._validate_uuid(chat_id, "chat_id")
        supabase = get_supabase()
        result = (
            supabase.table("messages")
            .select("*")
            .eq("chat_id", chat_id)
            .order("created_at", desc=False)
            .execute()
        )
        return [StoredMessageResponse(**row) for row in result.data]
