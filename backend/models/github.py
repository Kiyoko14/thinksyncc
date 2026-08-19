from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# GitHub Connection (credential vault entry)
# ---------------------------------------------------------------------------
#
# SECURITY MODEL (matches ThinkSync's existing server-credential contract):
#   - Only SSH auth is supported. No PAT/token in this layer yet.
#   - The private key is ENCRYPTED AT REST (core/crypto.encrypt_secret) and is
#     NEVER returned to the client. Only the public key + metadata are exposed.
#   - The decrypted key lives only in process memory, passed directly to git's
#     SSH_COMMAND for a single operation, then discarded (no on-disk file).
#   - github_push requires explicit approval + a preview before pushing.
# ---------------------------------------------------------------------------


class GitHubAuthMethod(str):
    """Allowed GitHub authentication methods (connection layer)."""

    SSH = "ssh"


class GitHubConnectionCreate(BaseModel):
    """Request body to create a GitHub connection.

    Provide EXACTLY ONE of:
      - ``ssh_private_key`` + ``ssh_public_key`` (import an existing keypair)
      - ``generate_keypair=True`` (server generates one for you)
    """

    name: str = Field(..., min_length=1, max_length=120)
    auth_method: str = Field(default="ssh", pattern="^ssh$")
    # Import-mode: caller supplies both keys (private key is encrypted at rest).
    ssh_private_key: Optional[str] = Field(default=None, description="PEM/PKCS#8/OPENSSH private key. Encrypted at rest. Never returned.")
    ssh_public_key: Optional[str] = Field(default=None, description="Public key (id_rsa.pub). Returned to the user so they can add it to GitHub.")
    # Generation-mode: server creates a fresh ed25519 keypair.
    generate_keypair: bool = Field(default=False, description="If true, server generates a new ed25519 keypair and returns the public key once.")
    # Optional: which GitHub host to talk to (default github.com).
    host: str = Field(default="github.com", min_length=3, max_length=253)


class GitHubConnectionResponse(BaseModel):
    """Public view of a connection. NEVER includes the private key."""

    id: str
    user_id: str
    name: str
    auth_method: str
    host: str
    ssh_public_key: Optional[str] = None
    ssh_key_type: Optional[str] = None
    # Repositories linked to this connection (populated on demand, not stored).
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class GitHubConnectionWithKey(GitHubConnectionResponse):
    """Like GitHubConnectionResponse but INCLUDES the freshly generated
    private key. Returned ONLY once, at creation time, when generate_keypair=True.
    The caller MUST persist it (GitHub side) immediately — ThinkSync never
    stores or returns it again."""

    ssh_private_key: Optional[str] = None


class GitHubRepoAccessRequest(BaseModel):
    """Validate that a connection can reach a given repository."""

    repo: str = Field(..., min_length=1, max_length=200, description="owner/name, e.g. 'nous-research/hermes'")
    # Optional branch to verify existence of (defaults to resolving default branch).
    ref: Optional[str] = None


class GitHubRepoAccessResponse(BaseModel):
    reachable: bool
    repo: str
    resolved_ref: Optional[str] = None
    default_branch: Optional[str] = None
    can_read: bool = False
    can_write: bool = False
    message: str = ""


class GitHubRepoMetadata(BaseModel):
    """Lightweight repository metadata (no clone)."""

    repo: str
    default_branch: str
    owner: str
    name: str
    ssh_url: str
    clone_supported: bool = True


class GitHubCloneRequest(BaseModel):
    """Clone a repository into a workspace during workspace creation.

    The clone uses the connection's SSH key (decrypted in-memory) and is
    performed by the BACKEND, never by the agent.
    """

    github_connection_id: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1, max_length=200)
    branch: Optional[str] = None  # None -> default branch
    depth: Optional[int] = Field(default=None, ge=1, le=1, description="Shallow clone depth (1 = single commit). None = full history.")


# ---------------------------------------------------------------------------
# Agent-side GitHub tools (Forge v2 step actions)
# ---------------------------------------------------------------------------


class GitHubPullRequest(BaseModel):
    """Agent request to pull (fetch + fast-forward / merge) in an existing workspace repo."""

    remote: str = Field(default="origin", min_length=1, max_length=40)
    branch: Optional[str] = None  # None -> current branch's upstream
    strategy: str = Field(default="ff_only", pattern="^(ff_only|merge|rebase)$")


class GitHubPushRequest(BaseModel):
    """Agent request to push. ALWAYS requires explicit human approval + preview."""

    remote: str = Field(default="origin", min_length=1, max_length=40)
    branch: Optional[str] = None  # None -> current branch
    force: bool = Field(default=False, description="Force push (--force-with-lease). Requires HIGH approval.")
    tags: bool = Field(default=False, description="Also push tags.")
