"""
Requirement Patch Engine — Objective 4 (Sprint 3B).

User replies MUST NOT rebuild the entire specification.

Pipeline (Objective 4):
    User Reply
        ↓
    Intent
        ↓
    RequirementPatch
        ↓
    Validation
        ↓
    Review
        ↓
    Approval
        ↓
    Freeze
        ↓
    Resume

Small changes must remain small.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from enum import Enum

import copy
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from models.agent import ProjectSpecification
from models.approval import FrozenSpecViolationError, ensure_frozen_spec_immutable
from models.conversation import ConversationSession, ConversationSessionStore
from services.conversation_reliability import OptimisticLockGuard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RequirementPatch
# ---------------------------------------------------------------------------

class PatchType(str, Enum):
    """Type of specification patch."""

    UPDATE_FIELD = "update_field"
    ADD_ITEM = "add_item"
    REMOVE_ITEM = "remove_item"
    UPDATE_ASSUMPTION = "update_assumption"
    UPDATE_ARCHITECTURE = "update_architecture"
    UPDATE_DEPLOYMENT = "update_deployment"


class RequirementPatch(BaseModel):
    """A single small patch to a ProjectSpecification.

    Small changes MUST remain small — never replace the entire spec.
    """

    patch_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:12])
    patch_type: PatchType = PatchType.UPDATE_FIELD

    # Which field/path this patch modifies
    target_path: str = ""  # e.g. "name", "assumptions[0].field"

    # Old value (for optimistic locking / idempotency)
    old_value: Any = None

    # New value
    new_value: Any = None

    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    applied: bool = False
    applied_at: datetime | None = None

    def apply(self, spec: ProjectSpecification) -> ProjectSpecification:
        """Apply this patch to a spec (returns a NEW spec, does NOT mutate)."""
        import json as _json
        spec_dict = json.loads(spec.model_dump_json())
        # Navigate to target path
        parts = self.target_path.split(".")
        current = spec_dict
        for part in parts[:-1]:
            if "[" in part:
                key, idx_str = part.split("[")
                idx = int(idx_str.rstrip("]"))
                current = current[key][idx]
            else:
                current = current[part]
        # Set the final field
        final_key = parts[-1]
        if "[" in final_key:
            key, idx_str = final_key.split("[")
            idx = int(idx_str.rstrip("]"))
            current[key][idx] = self.new_value
        else:
            current[final_key] = self.new_value
        # Rebuild spec from patched dict
        patched = ProjectSpecification(**spec_dict)
        self.applied = True
        self.applied_at = datetime.now(timezone.utc())
        return patched


# ---------------------------------------------------------------------------
# RequirementPatchEngine
# ---------------------------------------------------------------------------

class RequirementPatchEngine:
    """Applies small patches to a frozen specification.

    Enforces:
      - Small changes remain small (max 5 patches per session)
      - Patches are idempotent (patch_id tracked in session)
      - Frozen spec guard is always checked
    """

    MAX_PATCHES_PER_SESSION: int = 5

    @classmethod
    async def apply_patch(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        spec: ProjectSpecification,
        patch: RequirementPatch,
    ) -> ProjectSpecification:
        """Apply a single patch to the spec.

        Raises FrozenSpecViolationError if spec is frozen.
        Raises ValueError if too many patches in this session.
        """
        # Guard: ensure spec is NOT frozen
        ensure_frozen_spec_immutable(spec, context="requirement_patch")

        # Load session (for idempotency + patch count)
        session = await ConversationSessionStore.load_or_create(
            job_id, conversation_id,
        )

        # Idempotency: skip if patch already applied
        existing_ids = {
            p.get("patch_id") for p in session.patch_history
        }
        if patch.patch_id in existing_ids:
            logger.warning(
                "[patch] skipping already-applied patch %s",
                patch.patch_id,
            )
            return spec

        # Enforce max patches per session
        if len(session.patch_history) >= cls.MAX_PATCHES_PER_SESSION:
            raise ValueError(
                f"Too many patches in this session "
                f"(max {cls.MAX_PATCHES_PER_SESSION}). "
                f"Please restart the conversation."
            )

        # Apply patch
        patched_spec = patch.apply(spec)

        # Record in session (use atomic save)
        session.add_patch(patch.model_dump(mode="json"))
        await OptimisticLockGuard.save_session_atomic(session)

        logger.info(
            "[patch] applied patch %s to spec %s",
            patch.patch_id,
            getattr(spec, "name", "<unknown>"),
        )
        return patched_spec

    @classmethod
    async def apply_patches(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        spec: ProjectSpecification,
        patches: list[RequirementPatch],
    ) -> ProjectSpecification:
        """Apply multiple patches sequentially."""
        current = spec
        for patch in patches:
            current = await cls.apply_patch(
                job_id, conversation_id,
                spec=current,
                patch=patch,
            )
        return current
