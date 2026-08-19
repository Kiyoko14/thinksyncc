from datetime import datetime

from pydantic import BaseModel, Field


class WorkspaceCreateRequest(BaseModel):
    server_id: str
    name: str = Field(..., min_length=1, max_length=150)
    # Original, human-readable workspace name chosen by the user. Kept separate
    # from the internally sanitized `name`/`slug`. If omitted, the backend falls
    # back to `name`. Never overwritten by the slug.
    display_name: str | None = Field(default=None)
    # Optional GitHub integration: link an existing connection and clone its repo
    # into the new workspace. The BACKEND performs the clone (the agent never
    # clones). Leave both None for a plain empty workspace.
    github_connection_id: str | None = Field(default=None)
    github_repo: str | None = Field(default=None, description="owner/name")
    github_branch: str | None = Field(default=None)
    github_depth: int | None = Field(default=None, ge=1, le=1)
    # GitHub App (OAuth) clone request — selects the App-token HTTPS clone path.
    # Mutually exclusive with the SSH linkage above; the backend picks the
    # transport based on which field is present.
    app_clone: "GitHubAppCloneRequest | None" = Field(default=None)


class WorkspaceResponse(BaseModel):
    id: str
    user_id: str
    server_id: str
    name: str
    path: str
    slug: str
    domain: str
    url: str | None = None
    # Original user-provided workspace name (human-readable). Falls back to
    # `name` when null. Internal identifiers (slug/id) must never replace this.
    display_name: str | None = None
    github_connection_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
