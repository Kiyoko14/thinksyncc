from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ChatMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)


class ChatMessageDualRequest(BaseModel):
    workspace_id: str | None = Field(default=None)
    git_repo_id: str | None = Field(default=None)
    message: str = Field(..., min_length=1, max_length=20000)


class StoredMessageResponse(BaseModel):
    id: str
    chat_id: str
    role: ChatMessageRole
    content: str
    created_at: datetime

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


class ChatSendMessageDualResponse(BaseModel):
    chat_id: str
    workspace_id: str | None = None
    git_repo_id: str | None = None
    response: str
