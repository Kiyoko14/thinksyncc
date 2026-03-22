from datetime import datetime

from pydantic import BaseModel, Field


class WorkspaceCreateRequest(BaseModel):
    server_id: str
    name: str = Field(..., min_length=1, max_length=150)


class WorkspaceResponse(BaseModel):
    id: str
    user_id: str
    server_id: str
    name: str
    path: str
    slug: str
    domain: str
    created_at: datetime

    model_config = {"from_attributes": True}
