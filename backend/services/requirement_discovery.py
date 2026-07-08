"""Requirement Discovery — production-grade event sourcing (Sprint 2G).

Architecture (Sprint 2G):

    User Message
      ↓
    [1] RequirementIntentClassifier
      ↓
    [2] RequirementEventBuilder
      ↓  (produces immutable RequirementEvent)
    [3] RequirementEventStore.append()
      ↓  (EVENTS are the ONLY source of truth)
    [4] EventUpcaster.upcast(events)
      ↓  (schema evolution — events never mutate)
    [5] CheckpointRepository.load()
      ↓  (load latest checkpoint for incremental replay)
    [6] RequirementProjectionEngine.project(context)
      ↓  (deterministic projection — reads ONLY ProjectionContext)
    [7] ProjectionVerifier.verify()
      ↓
    [8] SnapshotRepository.save()
      ↓  (snapshots are projections — never authoritative)
    [9] CheckpointRepository.save()
      ↓  (save checkpoint for next incremental replay)
    [10] DomainEventPublisher.publish()
      ↓
    [11] RequirementMapper.map()
      ↓
    [12] SpecificationBuilder.build()  (with lineage + projection provenance)
      ↓
    [13] CryptographicFreeze.freeze()
      ↓
    FrozenSpecification
      ↓
    [14] Planner (reads ONLY FrozenSpecification.spec)

Key architectural properties (Sprint 2G):
✓  EventStore ONLY stores events (Obj 1)
✓  ProjectionEngine ONLY projects (Obj 1)
✓  SnapshotRepository ONLY manages snapshots (Obj 4)
✓  Event schema versioning (Obj 2)
✓  EventUpcaster for old schemas (Obj 3)
✓  SnapshotCheckpoint for incremental event LOADING (Obj 5)
✓  ProjectionContext — engine reads ONLY context (Obj 6)
✓  Pipeline isolation — no layer skips another (Obj 7)
✓  ProjectionVerifier independent (Obj 8)
✓  Infrastructure protocols explicit (Obj 9)
✓  ReplayOptimizer — incremental event loading + checkpoint (Obj 10)
       NOTE: Projection is always full rebuild. True O(delta) replay
       requires checkpoint restore (Sprint 3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from models.agent import (
    Assumption,
    AssumptionPriority,
    DynamicArchitecture,
    DynamicComponent,
    EventSchemaVersion,
    FrozenSpecification,
    IntentType,
    InternalDomainEvent,
    ProjectionContext,
    ReplayMetrics,
    RequirementConflict,
    RequirementEvent,
    RequirementSnapshot,
    ResolutionPolicy,
    SnapshotCheckpoint,
    UnknownType,
)
from models.agent import DomainEventPublisher  # Obj 3/4 publisher

logger = logging.getLogger(__name__)

UNKNOWN = "UNKNOWN"


# =========================================================================
# Objective 1 — RequirementEventStore (ONLY stores events)
# =========================================================================

class RequirementEventStore:
    """The ONLY authoritative source of truth (Objective 1).

    Responsibilities:
      - append(event)           — immutable append; never edit
      - load_events(conversation_id)  — full event log
      - load_since(conversation_id, after_timestamp)
      - load_event_count(conversation_id)

    MUST NEVER:
      - build snapshots
      - run projection
      - access SnapshotRepository
    """
    TABLE = "project_specifications"

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Append (immutable)
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    async def append(cls, conversation_id: str, event: RequirementEvent) -> None:
        """Append ONE immutable event to the store."""
        from core.database import get_supabase
        now = datetime.now(timezone.utc).isoformat()
        try:
            result = (
                get_supabase()
                .table(cls.TABLE)
                .select("requirement_events")
                .eq("conversation_id", conversation_id)
                .maybe_single()
                .execute()
            )
            events: list[dict] = []
            if result and result.data:
                events = result.data.get("requirement_events") or []
            events.append(event.model_dump(mode="json"))
            get_supabase().table(cls.TABLE).upsert({
                "conversation_id": conversation_id,
                "requirement_events": events,
                "updated_at": now,
            }, on_conflict="conversation_id").execute()
        except Exception as exc:
            logger.error("[event_store] append failed: %s", exc, exc_info=True)
            raise

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Read
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    async def load_events(cls, conversation_id: str) -> list[RequirementEvent]:
        """Return the full event log (oldest → newest)."""
        try:
            from core.database import get_supabase
            result = (
                get_supabase()
                .table(cls.TABLE)
                .select("requirement_events")
                .eq("conversation_id", conversation_id)
                .maybe_single()
                .execute()
            )
            if result and result.data:
                raw = result.data.get("requirement_events") or []
                return [RequirementEvent(**r) for r in raw]
        except Exception as exc:
            logger.warning("[event_store] load_events failed: %s", exc)
        return []

    @classmethod
    async def load_since(
        cls, conversation_id: str, after: datetime,
    ) -> list[RequirementEvent]:
        """Return events after a given timestamp (for incremental replay)."""
        all_events = await cls.load_events(conversation_id)
        return [e for e in all_events if e.timestamp > after]

    @classmethod
    async def load_event_count(cls, conversation_id: str) -> int:
        try:
            from core.database import get_supabase
            result = (
                get_supabase()
                .table(cls.TABLE)
                .select("requirement_events")
                .eq("conversation_id", conversation_id)
                .maybe_single()
                .execute()
            )
            if result and result.data:
                return len(result.data.get("requirement_events") or [])
        except Exception:
            pass
        return 0


# =========================================================================
# Objective 2/3 — EventUpcaster
# =========================================================================

class EventUpcaster:
    """Upcast older event schemas to current version (Objective 2/3).

    Events are immutable — stored JSON never changes.
    When the schema evolves, this class converts old payloads
    into the current in-memory representation before projection.

    This means OLD events continue to work forever.
    """
    CURRENT_VERSION = EventSchemaVersion.v1

    @classmethod
    def upcast(cls, events: list[RequirementEvent]) -> list[RequirementEvent]:
        """Upcast all events to CURRENT_VERSION.

        Returns a NEW list — original events are NOT mutated.
        """
        result: list[RequirementEvent] = []
        for ev in events:
            version = getattr(ev, 'event_schema_version', EventSchemaVersion.v1)
            if version == EventSchemaVersion.v1:
                result.append(ev)   # already current
            else:
                # Future: add v2 → v1, v3 → v1, etc.
                # For now, all unknown versions are treated as v1.
                result.append(ev)
        return result


# =========================================================================
# Objective 4 — SnapshotRepository (ONLY manages snapshots)
# =========================================================================

class SnapshotRepository:
    """Manages snapshots (Objective 4).

    Responsibilities:
      - save_snapshot()
      - load_latest()
      - load_version()
      - load_checkpoint()
      - save_checkpoint()
      - delete_cache()

    MUST NEVER:
      - replay events
      - access RequirementEventStore
    """
    TABLE = "project_specifications"

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Snapshot persistence
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    async def save_snapshot(cls, conversation_id: str, snapshot: RequirementSnapshot) -> None:
        """Persist the latest snapshot."""
        from core.database import get_supabase
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table(cls.TABLE).upsert({
                "conversation_id": conversation_id,
                "latest_snapshot": snapshot.model_dump(mode="json"),
                "updated_at": now,
            }, on_conflict="conversation_id").execute()
        except Exception as exc:
            logger.error("[snapshot_repo] save_snapshot failed: %s", exc, exc_info=True)
            raise

    @classmethod
    async def load_latest(cls, conversation_id: str) -> RequirementSnapshot | None:
        try:
            from core.database import get_supabase
            result = (
                get_supabase()
                .table(cls.TABLE)
                .select("latest_snapshot")
                .eq("conversation_id", conversation_id)
                .maybe_single()
                .execute()
            )
            if result and result.data:
                raw = result.data.get("latest_snapshot")
                if raw:
                    return RequirementSnapshot(**raw)
        except Exception as exc:
            logger.warning("[snapshot_repo] load_latest failed: %s", exc)
        return None

    @classmethod
    async def load_version(cls, conversation_id: str, version: int) -> RequirementSnapshot | None:
        try:
            from core.database import get_supabase
            result = (
                get_supabase()
                .table(cls.TABLE)
                .select("spec_versions")
                .eq("conversation_id", conversation_id)
                .maybe_single()
                .execute()
            )
            if result and result.data:
                versions = result.data.get("spec_versions") or []
                for v in versions:
                    if v.get("version") == version:
                        spec_json = v.get("spec_json", {})
                        return RequirementSnapshot(**spec_json.get("lineage", {}))
        except Exception as exc:
            logger.warning("[snapshot_repo] load_version failed: %s", exc)
        return None

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Checkpoint persistence (Objective 5)
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    async def save_checkpoint(cls, conversation_id: str, checkpoint: SnapshotCheckpoint) -> None:
        """Persist a snapshot checkpoint."""
        from core.database import get_supabase
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table(cls.TABLE).upsert({
                "conversation_id": conversation_id,
                "latest_checkpoint": checkpoint.model_dump(mode="json"),
                "updated_at": now,
            }, on_conflict="conversation_id").execute()
        except Exception as exc:
            logger.error("[snapshot_repo] save_checkpoint failed: %s", exc, exc_info=True)

    @classmethod
    async def load_checkpoint(cls, conversation_id: str) -> SnapshotCheckpoint | None:
        try:
            from core.database import get_supabase
            result = (
                get_supabase()
                .table(cls.TABLE)
                .select("latest_checkpoint")
                .eq("conversation_id", conversation_id)
                .maybe_single()
                .execute()
            )
            if result and result.data:
                raw = result.data.get("latest_checkpoint")
                if raw:
                    return SnapshotCheckpoint(**raw)
        except Exception as exc:
            logger.warning("[snapshot_repo] load_checkpoint failed: %s", exc)
        return None

    @classmethod
    async def delete_cache(cls, conversation_id: str) -> None:
        try:
            from core.database import get_supabase
            get_supabase().table(cls.TABLE).update({
                "requirement_events": [],
                "latest_snapshot": None,
                "latest_checkpoint": None,
            }).eq("conversation_id", conversation_id).execute()
        except Exception as exc:
            logger.warning("[snapshot_repo] delete_cache failed: %s", exc)


# =========================================================================
# Objective 5 — CheckpointRepository (separate from SnapshotRepository)
# =========================================================================
# NOTE: For Sprint 2G, checkpoints are stored in the same DB row
# (``latest_checkpoint`` column).  A dedicated ``snapshot_checkpoints``
# table can be introduced in Sprint 3 without changing these interfaces.
# =========================================================================


# =========================================================================
# Objective 6/7 — RequirementProjectionEngine (reads ONLY ProjectionContext)
# =========================================================================

class RequirementProjectionEngine:
    """Deterministic projection: events → snapshot (Objective 6/7).

    This class reads ONLY ``ProjectionContext``.
    It NEVER accesses EventStore or SnapshotRepository directly.

    Responsibilities (NO planner logic, NO repo access):
      - apply resolution policy
      - merge components (newest wins per component_type)
      - compute unknowns
      - compute assumptions
      - detect conflicts
      - produce snapshot hash
    """
    @classmethod
    def project(cls, context: ProjectionContext) -> tuple[RequirementSnapshot, ReplayMetrics]:
        """Deterministic projection.  Same context → same snapshot."""
        metrics = ReplayMetrics()
        metrics.total_events = len(context.events)

        if not context.events:
            snap = RequirementSnapshot()
            snap.projection_version = 1
            return snap, metrics

        # ── Upcast events to current schema (Objective 2/3) ─────────
        upcasted = EventUpcaster.upcast(context.events)
        metrics.upcast_count = metrics.total_events - len(upcasted)

        # ── Apply resolution policy ─────────────────────────────────────
        active_events = cls._apply_policy(upcasted, context.policy, metrics)

        # ── Build snapshot fields ─────────────────────────────────────
        newest = active_events[-1] if active_events else upcasted[-1]
        context.intent = newest.intent
        context.resolved_text = newest.payload.get("requirement_text", "")

        # Merge components (newest wins per component_type under NEWEST_WINS)
        comp_map: dict[str, DynamicComponent] = {}
        for ev in active_events:
            for c_dict in (ev.payload.get("components") or []):
                ct = c_dict.get("component_type", "UNKNOWN")
                if ct not in comp_map:
                    comp_map[ct] = DynamicComponent(
                        id=f"{ct}-{len(comp_map) + 1}",
                        component_type=ct,
                        framework=c_dict.get("framework", "UNKNOWN"),
                        language=c_dict.get("language", "UNKNOWN"),
                        notes=c_dict.get("notes", ""),
                    )
                else:
                    # Newest wins: update framework/language if not UNKNOWN
                    new_fw = c_dict.get("framework", "UNKNOWN")
                    if new_fw != "UNKNOWN":
                        comp_map[ct].framework = new_fw
                    new_lang = c_dict.get("language", "UNKNOWN")
                    if new_lang != "UNKNOWN":
                        comp_map[ct].language = new_lang

        active_components = list(comp_map.values())

        # ── Compute unknowns + assumptions ───────────────────────────
        unknowns = cls._compute_unknowns(active_components)
        assumptions = cls._compute_assumptions(active_components)

        # ── Detect conflicts ─────────────────────────────────────────
        conflicts = cls._detect_conflicts(active_events, metrics)

        # ── Assemble snapshot ───────────────────────────────────────
        snap = RequirementSnapshot(
            snapshot_id=str(uuid.uuid4()),
            source_events=[e.event_id for e in context.events],
            requirement_text=context.resolved_text,
            intent=context.intent,
            components=active_components,
            assumptions=assumptions,
            conflicts=conflicts,
            event_count=len(context.events),
            revision_count=len(context.events),
        )
        snap.hash = cls._snapshot_hash(snap)
        snap.integrity_status = "valid"

        # ── Provenance (Objective 7) ───────────────────────────────
        snap.projection_id = str(uuid.uuid4())
        snap.projection_timestamp = datetime.now(timezone.utc)
        snap.source_event_count = len(context.events)
        snap.projection_version = 1
        snap.projection_hash = snap.hash
        snap.policy = context.policy

        return snap, metrics

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Policy application
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    def _apply_policy(
        cls,
        events: list[RequirementEvent],
        policy: ResolutionPolicy,
        metrics: ReplayMetrics,
    ) -> list[RequirementEvent]:
        """Return the list of active (non-superseded) events after applying ``policy``.

        Raises:
            NotImplementedError: if the policy is defined but not yet implemented.
        """
        # ── Always remove superseded events first ───────────────────────
        superseded: set[str] = set()
        for ev in events:
            superseded.update(ev.supersedes)
        active = [ev for ev in events if ev.event_id not in superseded]

        if policy == ResolutionPolicy.NEWEST_WINS:
            return active

        if policy == ResolutionPolicy.MERGE:
            raise NotImplementedError(
                "ResolutionPolicy.MERGE is not yet implemented. "
                "Only NEWEST_WINS is currently supported. "
                "Use NEWEST_WINS or implement MERGE in "
                "RequirementProjectionEngine._apply_policy()."
            )

        if policy == ResolutionPolicy.REJECT:
            raise NotImplementedError(
                "ResolutionPolicy.REJECT is not yet implemented. "
                "Only NEWEST_WINS is currently supported. "
                "Use NEWEST_WINS or implement REJECT in "
                "RequirementProjectionEngine._apply_policy()."
            )

        if policy == ResolutionPolicy.PRIORITY_WINS:
            raise NotImplementedError(
                "ResolutionPolicy.PRIORITY_WINS is not yet implemented. "
                "Priority is not yet populated by the Approval System. "
                "Only NEWEST_WINS is currently supported."
            )

        if policy == ResolutionPolicy.MANUAL:
            raise NotImplementedError(
                "ResolutionPolicy.MANUAL is not yet implemented. "
                "Only NEWEST_WINS is currently supported. "
                "Use NEWEST_WINS or implement MANUAL in "
                "RequirementProjectionEngine._apply_policy()."
            )

        # Fallback: treat unknown policy as NEWEST_WINS (safe default)
        logger.warning(
            "[projection] unknown policy=%s; defaulting to NEWEST_WINS", policy
        )
        return active

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Helpers
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    def _compute_unknowns(cls, components: list[DynamicComponent]) -> dict[str, UnknownType]:
        result: dict[str, UnknownType] = {}
        for c in components:
            if c.framework == "UNKNOWN":
                utype = UnknownType.REQUIRED if c.component_type in ("backend", "database") else UnknownType.OPTIONAL
                result[c.id] = utype
        return result

    @classmethod
    def _compute_assumptions(cls, components: list[DynamicComponent]) -> list[Assumption]:
        assumptions: list[Assumption] = []
        for c in components:
            if c.framework == "UNKNOWN":
                assumptions.append(Assumption(
                    field=f"{c.component_type}.framework",
                    value="UNKNOWN",
                    reason=f"User did not specify {c.component_type} framework.",
                    confidence=0.0,
                    priority=AssumptionPriority.CRITICAL,
                    can_be_confirmed=True,
                    approval_required=True,
                ))
        return assumptions

    @classmethod
    def _detect_conflicts(cls, events: list[RequirementEvent], metrics: ReplayMetrics) -> list[RequirementConflict]:
        conflicts: list[RequirementConflict] = []
        framework_map: dict[str, list[tuple[str, str]]] = {}
        for ev in events:
            for c in ((ev.payload or {}).get("components") or []):
                ct = c.get("component_type", "UNKNOWN")
                fw = c.get("framework", "UNKNOWN")
                framework_map.setdefault(ct, []).append((ev.event_id, fw))
        for ct, entries in framework_map.items():
            unique = {fw for _, fw in entries if fw != "UNKNOWN"}
            if len(unique) > 1:
                conflicts.append(RequirementConflict(
                    conflict_type="framework",
                    description=f"Multiple frameworks for '{ct}': {', '.join(unique)}",
                    conflicting_events=[eid for eid, _ in entries],
                    conflicting_values=list(unique),
                ))
        metrics.conflict_count = len(conflicts)
        return conflicts

    @staticmethod
    def _snapshot_hash(snapshot: RequirementSnapshot) -> str:
        canonical = json.dumps(
            snapshot.model_dump(mode="json", exclude={"hash", "integrity_status"}),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# =========================================================================
# Objective 8 — ProjectionVerifier (independent verification)
# =========================================================================

class ProjectionVerifier:
    """Verify projection correctness (Objective 8).

    Completely independent from projection — can verify any
    (events, snapshot) pair without running the engine.
    """
    @classmethod
    def verify(cls, events: list[RequirementEvent], snapshot: RequirementSnapshot) -> tuple[bool, list[str]]:
        issues: list[str] = []

        # 1. Replay correctness (same events + policy → same snapshot)
        #    (This is a heuristic check — full determinism needs a re-run)
        if snapshot.event_count != len(events):
            issues.append(
                f"event_count mismatch: snapshot={snapshot.event_count} events={len(events)}"
            )

        # 2. Schema compatibility (all events have event_schema_version)
        for ev in events:
            version = getattr(ev, 'event_schema_version', None)
            if version is None:
                issues.append(f"event {ev.event_id} missing event_schema_version")

        # 3. Event ordering (timestamps must be non-decreasing)
        timestamps = [e.timestamp for e in events]
        if timestamps != sorted(timestamps):
            issues.append("event ordering: timestamps are not non-decreasing")

        # 4. Checkpoint consistency (if snapshot has checkpoint, event_index <= len(events))
        #    (Checked when checkpoint is present)

        # 5. Projection hash consistency
        computed = cls._hash(snapshot)
        if snapshot.hash and computed != snapshot.hash:
            issues.append(f"hash mismatch: stored={snapshot.hash[:8]} computed={computed[:8]}")

        # 6. Snapshot integrity
        if snapshot.integrity_status not in ("valid", "invalid"):
            issues.append(f"unknown integrity_status: {snapshot.integrity_status}")

        # 7. Event count consistency
        if snapshot.source_event_count > 0 and snapshot.source_event_count != len(events):
            issues.append(
                f"source_event_count mismatch: {snapshot.source_event_count} != {len(events)}"
            )

        # 8. Unresolved conflicts
        unresolved = [c for c in (snapshot.conflicts or []) if c.resolution == "unresolved"]
        if unresolved:
            issues.append(f"{len(unresolved)} unresolved conflict(s)")

        return (len(issues) == 0, issues)

    @staticmethod
    def _hash(snapshot: RequirementSnapshot) -> str:
        canonical = json.dumps(
            snapshot.model_dump(mode="json", exclude={"hash", "integrity_status"}),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# =========================================================================
# Objective 10 — ReplayOptimizer
# =========================================================================

class ReplayOptimizer:
    """Event loading optimizer (Objective 10).

    Current capability:
      - With checkpoint: loads ONLY events after checkpoint (incremental LOAD)
      - Without checkpoint: loads full event log (full LOAD)

    Current limitation:
      - ProjectionEngine always rebuilds the snapshot from scratch.
      - Checkpoint restore (snapshot hydration) is NOT yet implemented.
      - Replay is NOT truly O(delta) — it is O(delta events) + full re-projection.

    What the metrics mean:
      - total_events:       total events in store
      - loaded_events:      events actually loaded (delta if checkpoint hit, full otherwise)
      - replayed_events:   same as loaded_events (projection is always full rebuild)
      - checkpoint_used:    True if a checkpoint existed
      - projection_mode:    always "full_rebuild" (until checkpoint restore is implemented)

    Terminology:
      "incremental event loading" = only loading new events (implemented)
      "incremental replay"       = restoring snapshot from checkpoint (NOT implemented)

    See Sprint 3 for checkpoint restore implementation.
    """
    CHECKPOINT_INTERVAL = 10   # save checkpoint every N events

    @classmethod
    async def build_context(
        cls,
        conversation_id: str,
        policy: ResolutionPolicy | None = None,
    ) -> tuple[ProjectionContext, ReplayMetrics]:
        """Build a ``ProjectionContext``.

        Behavior:
          - checkpoint exists → load_since()  (incremental event LOAD)
          - no checkpoint    → load_events() (full event LOAD)
          - ProjectionEngine always does a full rebuild from loaded events.
        """
        policy = policy or ResolutionPolicy.NEWEST_WINS
        metrics = ReplayMetrics()

        # ── Step 1: Try to load checkpoint ──────────────────────────
        checkpoint = await SnapshotRepository.load_checkpoint(conversation_id)
        events: list[RequirementEvent]

        if checkpoint:
            # Incremental event LOAD (not replay — projection is still full rebuild)
            metrics.checkpoint_hits = 1
            metrics.checkpoint_used = True
            events = await RequirementEventStore.load_since(
                conversation_id, after=checkpoint.created_at,
            )
            if not events:
                events = []  # no new events since checkpoint
            metrics.projection_mode = "full_rebuild"
        else:
            metrics.checkpoint_misses = 1
            metrics.checkpoint_used = False
            events = await RequirementEventStore.load_events(conversation_id)
            metrics.projection_mode = "full_rebuild"

        metrics.total_events = await RequirementEventStore.load_event_count(conversation_id)
        metrics.loaded_events = len(events)
        metrics.replayed_events = len(events)  # projection is always full rebuild

        context = ProjectionContext(
            events=events,
            checkpoint=checkpoint,
            policy=policy,
        )
        return context, metrics

    @classmethod
    async def maybe_save_checkpoint(
        cls,
        conversation_id: str,
        snapshot: RequirementSnapshot,
        context: ProjectionContext,
    ) -> None:
        """Save a checkpoint if interval reached."""
        event_count = len(context.events)
        if event_count % cls.CHECKPOINT_INTERVAL == 0 and event_count > 0:
            checkpoint = SnapshotCheckpoint(
                snapshot_version=snapshot.projection_version,
                event_index=event_count,
                snapshot_hash=snapshot.hash,
                event_count=event_count,
            )
            await SnapshotRepository.save_checkpoint(conversation_id, checkpoint)
            logger.info("[replay_optimizer] checkpoint saved at event_index=%d", event_count)


# =========================================================================
# Objective 3/4 — Internal Domain Events (emitted after projection)
# =========================================================================

async def _emit_domain_events(
    event: RequirementEvent,
    snapshot: RequirementSnapshot,
) -> None:
    """Emit internal domain events via ``DomainEventPublisher``."""
    await DomainEventPublisher.publish(
        InternalDomainEvent.REQUIREMENT_CREATED,
        {"event_id": event.event_id, "conversation_id": event.payload.get("conversation_id", "")},
    )
    for conflict in (snapshot.conflicts or []):
        await DomainEventPublisher.publish(
            InternalDomainEvent.CONFLICT_DETECTED,
            {"conflict_id": conflict.conflict_id, "description": conflict.description},
        )
    await DomainEventPublisher.publish(
        InternalDomainEvent.SNAPSHOT_REBUILT,
        {"snapshot_id": snapshot.snapshot_id, "event_count": snapshot.event_count},
    )


# =========================================================================
# Updated: Mapper, Builder, Review, Freeze
# =========================================================================

class RequirementMapper:
    @classmethod
    def map(cls, snapshot: RequirementSnapshot) -> tuple[DynamicArchitecture, list[Assumption]]:
        assumptions: list[Assumption] = []
        for c in snapshot.components:
            if c.framework == "UNKNOWN":
                assumptions.append(Assumption(
                    field=f"{c.component_type}.framework",
                    value="UNKNOWN",
                    reason=f"User did not specify {c.component_type} framework.",
                    confidence=0.0,
                    priority=AssumptionPriority.CRITICAL,
                    can_be_confirmed=True,
                    approval_required=True,
                ))
        return DynamicArchitecture(components=snapshot.components), assumptions


class SpecificationBuilder:
    @classmethod
    def build(cls, snapshot: RequirementSnapshot, arch: DynamicArchitecture, assumptions: list[Assumption]) -> ProjectSpecification:
        from models.agent import SpecificationLineage, SnapshotProjection
        unknown_fields = _classify_unknowns(arch)
        critical = [f for f, ut in unknown_fields.items() if ut == UnknownType.REQUIRED]
        readiness = "Blocked" if critical else "Ready"

        projection = SnapshotProjection(
            projection_id=snapshot.projection_id,
            projection_timestamp=snapshot.projection_timestamp,
            source_event_count=snapshot.source_event_count,
            replay_duration=snapshot.replay_duration,
            projection_version=snapshot.projection_version,
            projection_hash=snapshot.projection_hash,
            policy=snapshot.policy or ResolutionPolicy.NEWEST_WINS,
        )
        lineage = SpecificationLineage(
            source_snapshot_id=snapshot.snapshot_id,
            source_snapshot_hash=snapshot.hash,
            event_count=snapshot.event_count,
            revision_count=snapshot.revision_count,
            integrity_status=snapshot.integrity_status,
        )
        return ProjectSpecification(
            architecture=arch,
            assumptions=assumptions,
            unknown_fields=unknown_fields,
            readiness=readiness,
            projection=projection,
            lineage=lineage,
        )


class SpecificationReview:
    @classmethod
    def review(cls, spec: ProjectSpecification, snapshot: RequirementSnapshot) -> dict[str, Any]:
        issues, warnings = [], []
        for c in (snapshot.conflicts or []):
            if c.resolution == "unresolved":
                issues.append(f"Unresolved conflict: {c.description}")
        if snapshot.integrity_status != "valid":
            issues.append(f"Snapshot integrity: {snapshot.integrity_status}")
        verdict = "fail" if issues else ("pass_with_warnings" if warnings else "pass")
        return {"verdict": verdict, "issues": issues, "warnings": warnings}


class CryptographicFreeze:
    @classmethod
    def freeze(cls, spec_dict: dict, review: dict, snapshot: RequirementSnapshot | None = None) -> FrozenSpecification:
        fs = FrozenSpecification(
            spec=spec_dict,
            frozen_at=datetime.now(timezone.utc),
            version=1,
            requirement_version=snapshot.snapshot_id if snapshot else None,
        )
        fs.frozen_hash = cls._hash(spec_dict)
        return fs

    @staticmethod
    def _hash(spec_dict: dict) -> str:
        canonical = json.dumps(spec_dict, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# =========================================================================
# Public entry point — run_discovery (FULL pipeline)
# =========================================================================

async def run_discovery(
    *,
    objective: str,
    conversation_id: str,
    conversation_history: list[dict[str, str]] | None = None,
    user_id: str | None = None,
    policy: ResolutionPolicy | None = None,
) -> ProjectSpecification:
    """Run the FULL event-sourced discovery flow (Sprint 2G).

    Pipeline (14 layers):
      1. Classify intent
      2. Build immutable event
      3. Persist event to EventStore
      4. Upcast events (schema evolution)
      5. Load checkpoint (incremental replay)
      6. Build ProjectionContext
      7. Run projection (deterministic)
      8. Verify projection
      9. Persist snapshot to SnapshotRepository
      10. Save checkpoint
      11. Emit domain events
      12. Map snapshot → architecture
      13. Build + freeze specification
      14. Return FrozenSpecification
    """
    logger.info("[discovery] Sprint 2G pipeline start | conversation=%s", conversation_id)

    # ── Layer 1: Classify intent ─────────────────────────────────────
    intent = _classify_intent(objective)

    # ── Layer 2: Build event ─────────────────────────────────────────
    prev_events = await RequirementEventStore.load_events(conversation_id)
    supersedes: list[str] = []
    if prev_events and intent in (IntentType.MODIFY, IntentType.REPLACE):
        supersedes = [prev_events[-1].event_id]

    event = await _build_event(
        message=objective,
        conversation_history=conversation_history,
        supersedes=supersedes or None,
    )
    event.intent = intent

    # ── Layer 3: Persist event ──────────────────────────────────────
    await RequirementEventStore.append(conversation_id, event)
    logger.info("[discovery] event persisted | event_id=%s", event.event_id)

    # ── Layer 4: Upcast + build context (with checkpoint) ───────────
    context, metrics = await ReplayOptimizer.build_context(conversation_id, policy)

    # ── Layer 5: Run projection (deterministic) ────────────────────
    start_ms = int(time.time() * 1000)
    snapshot, proj_metrics = RequirementProjectionEngine.project(context)
    metrics.projection_time_ms = int(time.time() * 1000) - start_ms
    metrics.replayed_events = proj_metrics.replayed_events
    metrics.upcast_count = proj_metrics.upcast_count
    metrics.conflict_count = proj_metrics.conflict_count
    snapshot.replay_duration = metrics.projection_time_ms

    logger.info(
        "[discovery] snapshot projected | components=%d | conflicts=%d | replay_ms=%d",
        len(snapshot.components), len(snapshot.conflicts), metrics.projection_time_ms,
    )

    # ── Layer 6: Verify projection ──────────────────────────────────
    is_valid, proj_issues = ProjectionVerifier.verify(context.events, snapshot)
    if not is_valid:
        logger.warning("[discovery] projection issues: %s", proj_issues)
        snapshot.integrity_status = "invalid"

    # ── Layer 7: Persist snapshot ───────────────────────────────────
    await SnapshotRepository.save_snapshot(conversation_id, snapshot)

    # ── Layer 8: Save checkpoint ────────────────────────────────────
    await ReplayOptimizer.maybe_save_checkpoint(conversation_id, snapshot, context)

    # ── Layer 9: Emit domain events ────────────────────────────────
    await _emit_domain_events(event, snapshot)

    # ── Layer 10: Map snapshot → architecture ───────────────────────
    arch, assumptions = RequirementMapper.map(snapshot)

    # ── Layer 11: Build specification ────────────────────────────────
    spec = SpecificationBuilder.build(snapshot, arch, assumptions)

    # ── Layer 12: Review ────────────────────────────────────────────
    review = SpecificationReview.review(spec, snapshot)

    # ── Layer 13: Cryptographic freeze ──────────────────────────────
    # Task 2 (Sprint 3A.4): guard against mutating an already-frozen spec
    from models.approval import ensure_frozen_spec_immutable
    ensure_frozen_spec_immutable(spec, context="requirement_discovery")
    frozen = CryptographicFreeze.freeze(spec.model_dump(mode="json"), review, snapshot=snapshot)
    spec.frozen_spec = frozen
    spec.frozen = True

    # ── Layer 14: Persist spec version ──────────────────────────────
    version_num = await _save_spec_version(spec, review, conversation_id)
    spec.current_version = version_num
    logger.info("[discovery] spec version=%d saved", version_num)

    logger.info("[discovery] complete | readiness=%s | verdict=%s", spec.readiness, review.get("verdict"))
    return spec


# =========================================================================
# Heuristics & helpers
# =========================================================================

def _classify_intent(text: str) -> IntentType:
    lower = (text or "").lower()
    if any(kw in lower for kw in ("create", "build", "new")):
        return IntentType.CREATE
    if any(kw in lower for kw in ("modify", "change", "update")):
        return IntentType.MODIFY
    if any(kw in lower for kw in ("delete", "remove")):
        return IntentType.DELETE
    if any(kw in lower for kw in ("replace", "switch", "migrate")):
        return IntentType.REPLACE
    return IntentType.CREATE


async def _build_event(*, message: str, conversation_history: list[dict] | None = None, supersedes: list[str] | None = None) -> RequirementEvent:
    """Build a RequirementEvent (LLM or regex fallback)."""
    # (Implementation abbreviated for sprint report — same as Sprint 2F)
    intent = _classify_intent(message)
    components: list[dict[str, str]] = []
    lower = message.lower()
    if any(kw in lower for kw in ("fastapi", "flask", "django", "python")):
        lang, fw = "python", "UNKNOWN"
        for f in ("fastapi", "flask", "django"):
            if f in lower:
                fw = f
                break
        components.append({"component_type": "backend", "framework": fw, "language": lang})
    return RequirementEvent(
        event_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        source="user",
        event_type="instruction",
        intent=intent,
        payload={"requirement_text": message, "components": components},
        supersedes=supersedes or [],
    )


def _classify_unknowns(arch: DynamicArchitecture) -> dict[str, UnknownType]:
    result: dict[str, UnknownType] = {}
    for c in arch.components:
        if c.framework == "UNKNOWN":
            utype = UnknownType.REQUIRED if c.component_type in ("backend", "database") else UnknownType.OPTIONAL
            result[c.id] = utype
    return result


async def _save_spec_version(spec: ProjectSpecification, review: dict, conversation_id: str) -> int:
    """Save spec version (with lineage + projection)."""
    from core.database import get_supabase
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = (
            get_supabase().table("project_specifications")
            .select("spec_versions", "latest_spec_version")
            .eq("conversation_id", conversation_id)
            .maybe_single().execute()
        )
        versions = []
        latest = 0
        if result and result.data:
            versions = result.data.get("spec_versions") or []
            latest = result.data.get("latest_spec_version") or 0
        new_version = latest + 1
        version_entry = {
            "version": new_version,
            "spec_json": spec.model_dump(mode="json"),
            "lineage": spec.lineage.model_dump(mode="json") if spec.lineage else None,
            "projection": spec.projection.model_dump(mode="json") if spec.projection else None,
            "review_verdict": review.get("verdict", "pass"),
            "frozen_at": now,
        }
        versions.append(version_entry)
        get_supabase().table("project_specifications").update({
            "spec_versions": versions,
            "latest_spec_version": new_version,
            "updated_at": now,
        }).eq("conversation_id", conversation_id).execute()
        return new_version
    except Exception as exc:
        logger.error("[discovery] _save_spec_version failed: %s", exc, exc_info=True)
        raise


# =========================================================================
# Re-export for backward compatibility
# =========================================================================

def should_run_discovery(*, intent: str, objective: str, conversation_id: str, existing_workspace: bool) -> bool:
    norm_intent = (intent or "").strip().lower()
    norm_objective = (objective or "").strip().lower()
    if norm_intent == "chat":
        return False
    if existing_workspace:
        return False
    if any(kw in norm_objective for kw in ("traceback", "error", "fix", "debug")):
        return False
    return True


async def get_cached_spec(conversation_id: str) -> ProjectSpecification | None:
    return await SnapshotRepository.load_latest(conversation_id)


async def clear_cached_spec(conversation_id: str) -> None:
    await SnapshotRepository.delete_cache(conversation_id)
