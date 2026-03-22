from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SSHAuthMethod(str, Enum):
    PASSWORD = "password"
    KEY = "key"


class ServerBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    host: str = Field(..., min_length=1, max_length=255)
    ssh_user: str = Field(..., min_length=1, max_length=100)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_auth_method: SSHAuthMethod


class ServerCreate(ServerBase):
    ssh_key: Optional[str] = None
    ssh_password: Optional[str] = None


class ServerResponse(ServerBase):
    id: str
    user_id: str
    created_at: datetime

    model_config = {"from_attributes": True}
