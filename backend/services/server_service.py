from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.database import get_supabase
from models.server import ServerCreate, ServerResponse


class ServerService:
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
    def list_servers(user_id: str) -> list[ServerResponse]:
        supabase = get_supabase()
        result = (
            supabase.table("servers")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        return [ServerResponse(**row) for row in result.data]

    @staticmethod
    def get_server(server_id: str, user_id: str) -> dict[str, Any]:
        ServerService._validate_uuid(server_id, "server_id")
        ServerService._validate_uuid(user_id, "user_id")
        supabase = get_supabase()
        try:
            result = (
                supabase.table("servers")
                .select("*")
                .eq("id", server_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except APIError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Server not found",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Server not found",
            )

        return result.data

    @staticmethod
    def create_server(user_id: str, data: ServerCreate) -> ServerResponse:
        ServerService._validate_uuid(user_id, "user_id")
        supabase = get_supabase()
        record = {
            "user_id": user_id,
            "name": data.name,
            "host": data.host,
            "ssh_user": data.ssh_user,
            "ssh_port": data.ssh_port,
            "ssh_auth_method": data.ssh_auth_method.value,
            "ssh_key": data.ssh_key,
            "ssh_password": data.ssh_password,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = supabase.table("servers").insert(record).execute()
        except APIError as exc:
            code = ServerService._api_error_code(exc)
            if code in {"22P02"}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid request data",
                )
            if code in {"42501", "23503"}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not allowed to create server",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create server",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create server",
            )

        return ServerResponse(**result.data[0])

    @staticmethod
    def delete_server(server_id: str, user_id: str) -> None:
        supabase = get_supabase()
        result = (
            supabase.table("servers")
            .delete()
            .eq("id", server_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Server not found",
            )
