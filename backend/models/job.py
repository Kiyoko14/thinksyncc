from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_LLM = "waiting_for_llm"
    WAITING_FOR_USER = "waiting_for_user"
    PAUSED = "paused"
    APPROVED = "approved"
    RESUMED = "resumed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class JobCreate(BaseModel):
    workspace_id: str | None = None
    server_id: str
    mode: str | None = None
    objective: str = Field(..., min_length=3, max_length=1000)
    # Original, human-readable workspace name (if the user explicitly named the
    # workspace). Optional; when absent the backend falls back to `objective`.
    # Internal identifiers (slug/id) must never replace this in user-facing text.
    display_name: str | None = None
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
    execution_result: dict[str, Any] | None = None
    # Frontend Synchronization: when the job is waiting for clarification the
    # actual question(s) MUST be visible (never a bare "waiting_for_user").
    clarification: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
