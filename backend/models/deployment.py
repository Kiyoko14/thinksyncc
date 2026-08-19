from datetime import datetime

from pydantic import BaseModel


class DeploymentResponse(BaseModel):
    workspace_id: str
    port: int | None = None
    domain: str
    slug: str
    is_active: bool
    runtime: str = "python"
    verified: bool = False


class DeploymentDetailResponse(BaseModel):
    workspace_id: str
    port: int | None = None
    domain: str
    slug: str
    is_active: bool
    runtime: str = "python"
    verified: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}
