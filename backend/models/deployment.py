from datetime import datetime

from pydantic import BaseModel


class DeploymentResponse(BaseModel):
    workspace_id: str
    port: int
    domain: str
    slug: str
    is_active: bool


class DeploymentDetailResponse(BaseModel):
    workspace_id: str
    port: int
    domain: str
    slug: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
