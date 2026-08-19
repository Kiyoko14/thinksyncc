# ThinkSync — Sprint 2F: Requirement Domain Stabilization

**Date:** 2026-07-04
**Goal:** Complete the Requirement Domain architecture by introducing deterministic event sourcing, internal domain events, and configurable resolution policies. No user-facing features. No planner redesign. No execution changes.

---

## Sprint Goal

Finalize the Requirement Domain so that every project requirement can be **replayed, reconstructed, audited, versioned, and deterministically resolved**.

This sprint strengthens the architecture that future systems (Approval, Planner, Template Intelligence, Execution, Audit) will rely on.

---

## Problems Solved

| # | Problem (Sprint 2E) | Fix (Sprint 2F) |
|---|---|---|
| 1 | `RequirementSnapshot` was stored as authoritative | **`RequirementEventStore`** — events are the ONLY source of truth; snapshots are rebuildable projections |
| 2 | No separation between storage and projection | **`RequirementProjectionEngine`** — deterministic projection: events → snapshot |
| 3 | No internal domain events | **`InternalDomainEvent`** enum + **`DomainEventPublisher`** — 9 internal events; NoOp by default |
| 4 | Resolution policy was hardcoded (newest wins) | **`ResolutionPolicy`** enum — 5 policies; **`RequirementResolutionContext`** |
| 5 | No resolution context | **`RequirementResolutionContext`** — policy + events + assumptions + conflicts + unknowns |
| 6 | Snapshot had no provenance | **`SnapshotProjection`** — `projection_id`, `projection_timestamp`, `source_event_count`, `replay_duration`, `projection_version`, `projection_hash`, `policy` |
| 7 | No projection verification | **`ProjectionVerifier`** — 5 checks: replay correctness, event ordering, hash consistency, projection integrity, conflict consistency |
| 8 | Domain service contracts unclear | **5 explicit protocols** — `EventStoreProtocol`, `ProjectionEngineProtocol`, `ProjectionVerifierProtocol`, `ResolutionPolicyProtocol`, `DomainEventPublisherProtocol` |
| 9 | Architectural leakage possible | **`_assert_no_leakage()` guard** — runtime check that Requirement domain never imports planner/executor/deployment/templates/approval/workers |

---

## Architecture — Before (Sprint 2E)

```
User Message
  ↓
RequirementEventBuilder
  ↓  (produces RequirementEvent)
RequirementResolutionEngine.resolve()
  ↓  (produces RequirementSnapshot)
RequirementMapper.map()
  ↓
SpecificationBuilder.build()
  ↓
FrozenSpecification
```

**Problems:**
- Snapshot was stored as authoritative (not rebuildable)
- No internal domain events
- Resolution policy hardcoded
- No projection provenance
- No projection verification
- Service contracts not explicit

---

## Architecture — After (Sprint 2F)

```
User Message (plain text)
  ↓
[1] RequirementIntentClassifier
  ↓
[2] RequirementEventBuilder
  ↓  (produces ONE immutable RequirementEvent)
[3] RequirementEventStore.append()
  ↓  (EVENTS are the ONLY source of truth)
[4] RequirementProjectionEngine.project()
  ↓  (deterministic: same events + policy → same snapshot)
[5] ProjectionVerifier.verify()
  ↓
[6] DomainEventPublisher.publish()
  ↓  (emits internal events: CONFLICT_DETECTED, SNAPSHOT_REBUILT, …)
[7] RequirementMapper.map()
  ↓
[8] SpecificationBuilder.build()
  ↓  (includes SnapshotProjection provenance)
[9] SpecificationReview.review()
  ↓
[10] CryptographicFreeze.freeze()
  ↓
[11] SpecificationRepository.save_spec_version()
  ↓  (includes lineage + projection)
FrozenSpecification
  ↓
[12] Planner (reads ONLY FrozenSpecification.spec)
```

**Improvements:**
- ✅ **Events are authoritative** — snapshots are projections (rebuildable)
- ✅ **Deterministic projection** — same events + policy → same snapshot
- ✅ **Internal domain events** — 9 event types; NoOp publisher by default
- ✅ **Configurable resolution policies** — 5 policies; injectable via context
- ✅ **Resolution context** — engine reads ONLY from context (no global state)
- ✅ **Projection provenance** — full metadata on every snapshot
- ✅ **Projection verification** — 5 independent checks
- ✅ **Domain service contracts** — 5 explicit protocols
- ✅ **Architectural isolation** — `_assert_no_leakage()` guard

---

## Objective 1 — True Event Store

**`RequirementEventStore`** — the ONLY authoritative source of truth.

```python
class RequirementEventStore:
    async def append(conversation_id, event)  # immutable append
    async def get_events(conversation_id)       # full event log
    async def get_events_since(conversation_id, after)  # incremental
    async def rebuild_snapshot(conversation_id, policy)  # deterministic replay
```

**Properties:**
- Events are **never edited** — immutable append only
- `rebuild_snapshot()` is **deterministic** — same events + same policy → same snapshot
- Snapshots are **never authoritative** — always derived from events
- Entire history is **replayable** — any past state can be reconstructed

---

## Objective 2 — Projection Architecture

**`RequirementProjectionEngine.project(context)`** — deterministic projection.

**Algorithm:**
1. Sort events by timestamp
2. Apply resolution policy (filter superseded events)
3. Merge components (newest wins per `component_type` under `NEWEST_WINS`)
4. Compute unknowns + assumptions
5. Detect conflicts
6. Assemble `RequirementSnapshot`
7. Compute integrity hash

**Properties:**
- **No planner logic** — pure data transformation
- **Deterministic** — same context → same snapshot
- **Replayable** — can rebuild from any point in event log

---

## Objective 3 — Domain Events

**`InternalDomainEvent`** — 9 internal event types:

| Event | When emitted |
|------|-------------|
| `REQUIREMENT_CREATED` | New requirement event appended |
| `REQUIREMENT_UPDATED` | Existing requirement modified |
| `REQUIREMENT_REMOVED` | Requirement removed |
| `REQUIREMENT_RESOLVED` | Resolution complete |
| `REQUIREMENT_FROZEN` | Snapshot frozen |
| `REQUIREMENT_ARCHIVED` | Snapshot archived |
| `CONFLICT_DETECTED` | Conflict detected during projection |
| `SNAPSHOT_REBUILT` | Snapshot rebuilt from events |
| `SPECIFICATION_GENERATED` | Specification generated from snapshot |

**Properties:**
- **Internal only** — not exposed as webhooks
- **No Event Bus** — in-memory pub/sub only
- **No runtime integrations** — NoOp by default
- **Future subscribers** — Approval, Planner, Templates, Audit will subscribe in Sprint 3+

---

## Objective 4 — Domain Event Publisher

**`DomainEventPublisher`** — in-memory pub/sub.

```python
class DomainEventPublisher:
    async def publish(event_type, payload)  # emit event
    def subscribe(event_type, handler)       # register subscriber
    def unregister(event_type, handler)     # remove subscriber
    def clear_all()                        # remove all (useful in tests)
```

**Default:** NoOp (nothing subscribed). Future systems subscribe in Sprint 3+.

---

## Objective 5 — Resolution Policies

**`ResolutionPolicy`** — configurable policies:

| Policy | Behavior |
|--------|-------------|
| `NEWEST_WINS` | Newest event wins per field/component (default) |
| `PRIORITY_WINS` | Highest-priority event wins (future: set by Approval) |
| `MANUAL` | Conflicts NOT resolved; user must decide |
| `MERGE` | Merge non-conflicting fields; keep conflicts |
| `REJECT` | Reject any event that creates a conflict |

**Injection:**
```python
context = RequirementResolutionContext(policy=ResolutionPolicy.MERGE)
snapshot = RequirementProjectionEngine.project(context)
```

---

## Objective 6 — Resolution Context

**`RequirementResolutionContext`** — bundle passed to projection engine.

```python
@dataclass
class RequirementResolutionContext:
    policy: ResolutionPolicy
    events: list[RequirementEvent]
    active_components: list[DynamicComponent]
    assumptions: list[Assumption]
    conflicts: list[RequirementConflict]
    unknowns: dict[str, UnknownType]
    metadata: dict[str, Any]
    # computed by engine:
    resolved_text: str
    intent: IntentType
    replay_duration_ms: int
    projection_version: int
```

**Properties:**
- Engine reads **ONLY** from context — no global state
- Engine calls **NO external APIs** — fully deterministic
- Engine imports **NO planner/executor** — architecturally isolated

---

## Objective 7 — Snapshot Provenance

**`SnapshotProjection`** — metadata about a projection run.

Every `RequirementSnapshot` now carries:

```python
snapshot.projection_id: str             # UUID of this projection run
snapshot.projection_timestamp: datetime  # when projection ran
snapshot.source_event_count: int         # how many events were replayed
snapshot.replay_duration: int           # milliseconds
snapshot.projection_version: int        # projection schema version
snapshot.projection_hash: str           # hash of snapshot at projection time
snapshot.policy: ResolutionPolicy       # which policy was used
```

**`ProjectSpecification` also carries:**
- `lineage: SpecificationLineage` (Objective 7 from Sprint 2E)
- `projection: SnapshotProjection` (Objective 7 from Sprint 2F)

---

## Objective 8 — Projection Verification

**`ProjectionVerifier.verify(events, snapshot)`** — 5 checks:

| Check | What it validates |
|-------|-------------------|
| Replay correctness | Same events + policy → same snapshot |
| Event ordering | Timestamp ordering preserved |
| Hash consistency | Stored hash matches computed hash |
| Projection integrity | No events lost in projection |
| Conflict consistency | Conflicts in snapshot match events |

**Result:** `(is_valid: bool, issues: list[str])`

---

## Objective 9 — Domain Contracts

**5 explicit protocols** (defined in `models/agent.py`):

| Protocol | Contract |
|----------|---------|
| `EventStoreProtocol` | `append()`, `get_events()`, `rebuild_snapshot()` |
| `ProjectionEngineProtocol` | `project(context) → RequirementSnapshot` |
| `ProjectionVerifierProtocol` | `verify(events, snapshot) → (bool, list[str])` |
| `ResolutionPolicyProtocol` | `resolve(events, context) → RequirementSnapshot` |
| `DomainEventPublisherProtocol` | `publish()`, `subscribe()`, `unregister()` |

**Property:** Each service owns **exactly one responsibility**. No duplicated logic.

---

## Objective 10 — Architectural Isolation

**`_assert_no_leakage()` guard** — runtime check.

The Requirement domain:

**MUST NEVER import:**
- ❌ `services.planner`
- ❌ `services.executor`
- ❌ `services.deployment`
- ❌ `services.templates`
- ❌ `services.approval`
- ❌ `services.workers`

**MUST ONLY export contracts:**
- ✅ `RequirementEvent`
- ✅ `RequirementSnapshot`
- ✅ `RequirementEventStore`
- ✅ `RequirementProjectionEngine`
- ✅ `ResolutionPolicy`
- ✅ `InternalDomainEvent`
- ✅ `DomainEventPublisher`

**Enforcement:** `_assert_no_leakage()` inspects the call stack at runtime and raises `RuntimeError` if leakage detected.

---

## Files Modified

| File | Changes |
|------|---------|
| `backend/models/agent.py` | **Major update** — `ResolutionPolicy`, `RequirementResolutionContext`, `RequirementEventStore`, `RequirementProjectionEngine`, `SnapshotProjection`, `InternalDomainEvent`, `DomainEventPublisher`, `ProjectionVerifier`, 5 domain protocols, `_assert_no_leakage()` guard |
| `backend/services/requirement_discovery.py` | **Complete rewrite** — 12-layer event-sourced pipeline; uses `RequirementEventStore`, `RequirementProjectionEngine`, `ProjectionVerifier`, `DomainEventPublisher` |
| `SPRINT_02F_REPORT.md` | **New** — this report |

---

## Backward Compatibility

| Item | Strategy |
|------|-----------|
| `ProjectSpecification` model | **Extended** (new `projection` field) — old fields kept |
| `run_discovery()` return type | **Unchanged** — returns `ProjectSpecification` |
| `should_run_discovery()` | **Unchanged** — same heuristic gate |
| DB table `project_specifications` | **Extended** — `requirement_events` column (added in Sprint 2E) now used as authoritative event store |
| Old `RequirementRevision` model | **Kept** (compatibility) — but no longer used in new pipeline |
| `DomainEventPublisher` | **NoOp by default** — no runtime behavior change |

---

## DB Migration

```sql
-- No new columns needed if Sprint 2E was deployed
-- (requirement_events column already exists)

-- Verify:
SELECT column_name
FROM information_schema.columns
WHERE table_name = 'project_specifications'
  AND column_name = 'requirement_events';

-- If missing, run:
ALTER TABLE project_specifications
  ADD COLUMN IF NOT EXISTS requirement_events jsonb DEFAULT '[]'::jsonb;
```

---

## Remaining Limitations

| Limitation | Plan |
|------|-----------|
| `DomainEventPublisher` is in-memory only (no persistence) | Sprint 3: persist domain events for audit UI |
| `ResolutionPolicy.PRIORITY_WINS` not fully implemented | Sprint 3: Approval System sets event priority |
| `ProjectionVerifier` does not detect ALL edge cases | Sprint 3: add more verification rules |
| `_assert_no_leakage()` is a runtime guard (not static) | Sprint 3: add CI lint rule to prevent leakage |
| DB migration must be run manually | Run ALTER TABLE before deploying Sprint 2F |

---

## Why the Requirement Domain is Now Complete

After Sprint 2F, the Requirement layer has ALL of:

1. ✅ **True event sourcing** — events are the ONLY source of truth
2. ✅ **Deterministic projection** — same events + policy → same snapshot
3. ✅ **Internal domain events** — 9 event types; NoOp by default
4. ✅ **Configurable resolution policies** — 5 policies; injectable
5. ✅ **Resolution context** — engine reads ONLY from context
6. ✅ **Snapshot provenance** — full metadata on every projection
7. ✅ **Projection verification** — 5 independent checks
8. ✅ **Domain service contracts** — 5 explicit protocols
9. ✅ **Architectural isolation** — `_assert_no_leakage()` guard
10. ✅ **Backward compatible** — no API changes, no UI changes

**Sprint 2 is officially CLOSED.**

---

## Preparation for Sprint 3

The following are **ready** for Sprint 3 integration:

1. **`DomainEventPublisher`** — Approval System subscribes to `CONFLICT_DETECTED`
2. **`ResolutionPolicy`** — Approval System sets `PRIORITY_WINS` after user approves
3. **`RequirementConflict`** — Approval System shows conflicts to user
4. **`SnapshotProjection`** — Audit UI shows projection provenance
5. **`ProjectionVerifier`** — Audit UI shows verification results
6. **`RequirementEventStore.rebuild_snapshot()`** — Audit UI replays any past state
7. **`InternalDomainEvent.SPECIFICATION_GENERATED`** — Template Intelligence subscribes to generate templates

**What Sprint 3 should NOT need to change:**
- `RequirementEventStore` — events are authoritative; complete
- `RequirementProjectionEngine` — deterministic projection; complete
- `RequirementIntentClassifier` — keyword-based; complete
- `DomainEventPublisher` — in-memory pub/sub; complete (Sprint 3 may add persistence)
- `ResolutionPolicy` — 5 policies defined; complete
- `ProjectionVerifier` — 5 checks; complete
- `_assert_no_leakage()` — runtime guard; complete
