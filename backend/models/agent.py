from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class AgentTier(str, Enum):
    FORGE_V1 = "forge-v1"


class AgentJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentPlanStep(BaseModel):
    step: int = Field(..., ge=1)
    command: str
    rationale: str
    approved: bool = True


class AgentRunRequest(BaseModel):
    server_id: str
    objective: str = Field(..., min_length=3, max_length=500)
    max_steps: int = Field(default=5, ge=1, le=10)
    allow_write: bool = False
    dry_run: bool = False
    step_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    max_concurrency: int | None = Field(default=None, ge=1, le=5)


class AgentExecutionResult(BaseModel):
    command: str
    output: str
    exit_code: int
    executed_at: datetime


class ArchitectureHistoryItem(BaseModel):
    id: str
    objective: str
    summary: str
    success: bool
    created_at: datetime


class AgentRunResponse(BaseModel):
    agent: AgentTier = AgentTier.FORGE_V1
    objective: str
    dry_run: bool
    policy: dict[str, str | int | bool]
    plan: list[AgentPlanStep]
    results: list[AgentExecutionResult]
    summary: str


class AgentOrchestrationRequest(BaseModel):
    workspace_id: str
    message: str = Field(..., min_length=3, max_length=20000)
    max_steps: int = Field(default=5, ge=1, le=10)
    allow_write: bool = False
    dry_run: bool = False
    step_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    max_concurrency: int | None = Field(default=None, ge=1, le=5)


class AgentOrchestrationResponse(BaseModel):
    agent: AgentTier = AgentTier.FORGE_V1
    chat_id: str
    user_message_id: str
    assistant_message_id: str
    run: AgentRunResponse
    architecture_history: list[ArchitectureHistoryItem]


class AgentPlanResponse(BaseModel):
    agent: AgentTier = AgentTier.FORGE_V1
    objective: str
    dry_run: bool = True
    policy: dict[str, str | int | bool]
    plan: list[AgentPlanStep]
    summary: str


class AgentAsyncRunAccepted(BaseModel):
    job_id: str
    status: AgentJobStatus = AgentJobStatus.QUEUED


class AgentJobResponse(BaseModel):
    job_id: str
    status: AgentJobStatus
    run: AgentRunResponse | None = None
    error: str | None = None
