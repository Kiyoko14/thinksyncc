import re
import shlex
from typing import Any

from fastapi import HTTPException, status


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
    def process_message(context: dict[str, Any], message: str, context_type: str = "workspace") -> str:
        # CRITICAL: Always use path, NEVER use name or domain
        context_path = AIService._validate_workspace_path(str(context.get("path", "")))
        context_name = context.get("name", context_type)

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
                    "and changing directories outside the context."
                )

        # CRITICAL: Scoped execution with ABSOLUTE PATH only
        safe_prefix = f"cd {shlex.quote(context_path)} &&"

        return (
            f"Simulated assistant response for {context_type} '{context_name}'. "
            f"Your message was received in the {context_type} context and is ready for next-step tooling. "
            f"Scoped execution: {safe_prefix} <command>. "
            f"Input: {cleaned_message}"
        )
