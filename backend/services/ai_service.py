import re
import shlex
from typing import Any

from fastapi import HTTPException, status
from openai import AsyncOpenAI

from core.config import get_settings


class AIService:
    _BLOCKED_PATTERNS = [
        re.compile(r"\bgit\s+clone\b", flags=re.IGNORECASE),
        re.compile(r"\bcd\s+\.\.(?:/|\\|\b)", flags=re.IGNORECASE),
    ]

    @staticmethod
    def _validate_workspace_path(path: str) -> str:
        cleaned = path.strip()
        if not cleaned or ".." in cleaned or not cleaned.startswith("/home/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid workspace path",
            )
        return cleaned

    @staticmethod
    async def process_message(workspace: dict[str, Any], message: str) -> str:
        # CRITICAL: Always use path, NEVER use name or domain
        workspace_path = AIService._validate_workspace_path(str(workspace.get("path", "")))
        workspace_name = workspace.get("name", "workspace")

        cleaned_message = message.strip()
        if not cleaned_message:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Message is required",
            )

        for pattern in AIService._BLOCKED_PATTERNS:
            if pattern.search(cleaned_message):
                return (
                    "I cannot run that request. Restricted actions include cloning repositories "
                    "and changing directories outside the workspace."
                )

        # CRITICAL: Scoped execution with ABSOLUTE PATH only
        safe_prefix = f"cd {shlex.quote(workspace_path)} &&"

        settings = get_settings()
        if not settings.OPENAI_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OPENAI_API_KEY is not configured",
            )

        client = AsyncOpenAI(
    api_key=settings.OPENAI_API_KEY,
    base_url=settings.OPENAI_BASE_URL,
)

        system_prompt = (
            "You are ThinkSync agent assistant for Linux server workspaces. "
            "Give concise, practical guidance. "
            "Never suggest destructive commands (rm -rf, mkfs, dd if=, reboot, shutdown). "
            f"Current workspace name: {workspace_name}. "
            f"All commands must be scoped with: {safe_prefix}"
        )

        try:
            response = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": cleaned_message},
                ],
            )
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to generate AI response",
            )

        output_text = ""
        if response.choices:
            output_text = response.choices[0].message.content or ""
        if not output_text:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Empty AI response",
            )

        return output_text.strip()
