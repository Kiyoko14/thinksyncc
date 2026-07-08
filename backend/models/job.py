from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_LLM = "waiting_for_llm"
    COMPLETED = "completed"
    FAILED = "failed"


class JobCreate(BaseModel):
    workspace_id: str | None = None
    server_id: str
    mode: str | None = None
    objective: str = Field(..., min_length=3, max_length=1000)
    max_steps: int = Field(default=8, ge=1, le=20)
    allow_write: bool | None = None
    dry_run: bool = False
    step_timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    conversation_id: str | None = None  # used by Requirement Discovery Engine (Sprint 2)


class JobAccepted(BaseModel):
    id: str
    status: JobStatus = JobStatus.QUEUED


class JobResponse(BaseModel):
    id: str
    user_id: str
    workspace_id: str | None = None
    server_id: str
    objective: str
    status: JobStatus
    allow_write: bool = False
    dry_run: bool = False
    intent: str = "chat"
    task_mode: str = "simple"
    plan: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    retries: list[dict[str, Any]] = []
    summary: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
