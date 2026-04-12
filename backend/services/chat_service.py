import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from models.chat import ChatMessageRole, StoredMessageResponse
from services.redis_service import RedisService
from services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)


class ChatService:
    MESSAGE_CONTEXT_LIMIT = 16

    @staticmethod
    def _redis_key(workspace_id: str) -> str:
        return f"chat:{workspace_id}"

    @staticmethod
    def _message_to_cache_payload(message: StoredMessageResponse) -> str:
        return json.dumps(
            {
                "id": message.id,
                "role": message.role.value,
                "content": message.content,
                "created_at": message.created_at.isoformat(),
                "chat_id": message.chat_id,
                "workspace_id": message.workspace_id,
                "user_id": message.user_id,
            }
        )

    @staticmethod
    def _hydrate_cached_messages(items: list[str]) -> list[StoredMessageResponse]:
        hydrated: list[StoredMessageResponse] = []
        for item in items:
            try:
                payload = json.loads(item)
                hydrated.append(StoredMessageResponse(**payload))
            except Exception:
                logger.warning("Skipping malformed cached chat payload")
        return hydrated

    @staticmethod
    def _append_message_to_redis(workspace_id: str, message: StoredMessageResponse) -> None:
        client = RedisService.get_sync_client()
        if client is None:
            return

        settings = get_settings()
        key = ChatService._redis_key(workspace_id)
        try:
            client.rpush(key, ChatService._message_to_cache_payload(message))
            client.ltrim(key, -settings.REDIS_CHAT_MEMORY_MAX_ITEMS, -1)
            if settings.REDIS_CHAT_MEMORY_TTL_SECONDS > 0:
                client.expire(key, settings.REDIS_CHAT_MEMORY_TTL_SECONDS)
        except Exception as exc:
            logger.warning("Failed to append workspace chat message to Redis: %s", exc)

    @staticmethod
    def _prime_workspace_messages_cache(workspace_id: str, messages: list[StoredMessageResponse]) -> None:
        client = RedisService.get_sync_client()
        if client is None:
            return

        settings = get_settings()
        key = ChatService._redis_key(workspace_id)
        payloads = [ChatService._message_to_cache_payload(message) for message in messages]
        try:
            client.delete(key)
            if payloads:
                client.rpush(key, *payloads)
                client.ltrim(key, -settings.REDIS_CHAT_MEMORY_MAX_ITEMS, -1)
            if settings.REDIS_CHAT_MEMORY_TTL_SECONDS > 0:
                client.expire(key, settings.REDIS_CHAT_MEMORY_TTL_SECONDS)
        except Exception as exc:
            logger.warning("Failed to prime workspace chat cache: %s", exc)

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
    def save_workspace_message(workspace_id: str, user_id: str, role: str, content: str) -> StoredMessageResponse:
        ChatService._validate_uuid(workspace_id, "workspace_id")
        ChatService._validate_uuid(user_id, "user_id")

        WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

        if role not in {ChatMessageRole.USER.value, ChatMessageRole.ASSISTANT.value, ChatMessageRole.SYSTEM.value}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid message role")

        cleaned_content = content.strip()
        if not cleaned_content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message content is required")

        supabase = get_supabase()
        payload = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "role": role,
            "content": cleaned_content,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = supabase.table("chat_messages").insert(payload).execute()
        except APIError as exc:
            code = ChatService._api_error_code(exc)
            if code in {"23503", "42501"}:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
            if code == "22P02":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request data")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to save workspace message",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to save workspace message",
            )

        stored = StoredMessageResponse(**result.data[0])
        ChatService._append_message_to_redis(workspace_id, stored)
        return stored

    @staticmethod
    def list_workspace_messages(workspace_id: str, user_id: str, limit: int | None = None) -> list[StoredMessageResponse]:
        ChatService._validate_uuid(workspace_id, "workspace_id")
        ChatService._validate_uuid(user_id, "user_id")

        WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

        client = RedisService.get_sync_client()
        if client is not None:
            try:
                cached_items = client.lrange(ChatService._redis_key(workspace_id), 0, -1)
            except Exception as exc:
                logger.warning("Failed to read workspace chat cache: %s", exc)
                cached_items = []
            if cached_items:
                messages = ChatService._hydrate_cached_messages(cached_items)
                if limit is not None:
                    return messages[-limit:]
                return messages

        query = (
            get_supabase()
            .table("chat_messages")
            .select("*")
            .eq("workspace_id", workspace_id)
            .eq("user_id", user_id)
            .order("created_at", desc=False)
        )
        if limit is not None:
            query = query.limit(limit)

        result = query.execute()
        messages = [StoredMessageResponse(**row) for row in result.data or []]
        if messages:
            ChatService._prime_workspace_messages_cache(workspace_id, messages)
        return messages

    @staticmethod
    def get_recent_context_messages(
        workspace_id: str,
        user_id: str,
        *,
        limit: int | None = None,
        current_input: str | None = None,
    ) -> list[dict[str, str]]:
        max_items = limit or ChatService.MESSAGE_CONTEXT_LIMIT
        messages = ChatService.list_workspace_messages(workspace_id=workspace_id, user_id=user_id)
        trimmed = messages[-max_items:]

        cleaned_current = (current_input or "").strip()
        if trimmed and cleaned_current:
            last_message = trimmed[-1]
            if last_message.role == ChatMessageRole.USER and last_message.content.strip() == cleaned_current:
                trimmed = trimmed[:-1]

        return [
            {"role": message.role.value, "content": message.content}
            for message in trimmed
        ]

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
