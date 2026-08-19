from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ChatMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)
    role: ChatMessageRole = ChatMessageRole.USER


class StoredMessageResponse(BaseModel):
    id: str
    role: ChatMessageRole
    content: str
    created_at: datetime
    chat_id: str | None = None
    workspace_id: str | None = None
    user_id: str | None = None

    model_config = {"from_attributes": True}


class ChatResponse(BaseModel):
    id: str
    workspace_id: str
    user_id: str
    created_at: datetime
    messages: list[StoredMessageResponse] = []

    model_config = {"from_attributes": True}


class ChatSendMessageResponse(BaseModel):
    chat_id: str
    workspace_id: str
    response: str
    message: StoredMessageResponse
