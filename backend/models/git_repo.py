from datetime import datetime

from pydantic import BaseModel, Field


class GitConnectRequest(BaseModel):
    workspace_id: str
    url: str = Field(..., min_length=10, max_length=2000)
    branch: str = Field(default="main", min_length=1, max_length=100)


class GitRepoResponse(BaseModel):
    id: str
    workspace_id: str
    user_id: str
    provider: str
    url: str
    branch: str
    is_cloned: bool
    created_at: datetime

    model_config = {"from_attributes": True}
