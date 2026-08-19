from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class CommandRequest(BaseModel):
    workspace_id: str | None = None
    server_id: str
    command: str


class CommandResponse(BaseModel):
    server_id: str
    command: str
    stdout: str = ""
    stderr: str = ""
    output: str
    exit_code: int
    executed_at: datetime
