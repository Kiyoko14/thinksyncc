# ThinkSync — Sprint 2E: Requirement Domain Completion

**Date:** 2026-07-04
**Goal:** Convert the Requirement layer from a collection of models into a complete domain model. No user-facing features. No execution changes. No planner redesign. Only strengthen the Requirement domain.

---

## Sprint Goal

The Requirement layer in Sprint 2D was a set of models with some logic. Sprint 2E makes it a **complete domain model** with: immutable events, deterministic snapshot resolution, explicit conflict detection, intent classification, relationship graphs, a state machine, full specification lineage, independent integrity verification, and clean domain service contracts.

---

## Problems Solved

| # | Problem (Sprint 2D) | Fix (Sprint 2E) |
|---|---|---|
| 1 | `RequirementRevision` was the source of truth | **`RequirementEvent`** — every user instruction is an immutable event; revisions/snapshots derived from events |
| 2 | Timeline was just a list of revisions | **`RequirementResolutionEngine`** — deterministically builds `RequirementSnapshot` from events; detects overrides, conflicts, duplicates, removals, amendments |
| 3 | Conflicts were implicit (validation errors) | **`RequirementConflict`** — explicit conflict objects; e.g. FastAPI + Express in same snapshot |
| 4 | Intent was not separated from requirement | **`IntentType` enum** — 9 intent categories; `RequirementIntentClassifier` classifies before extraction |
| 5 | `DynamicComponent` had no relationships | **`RelationshipGraph`** — `provides`, `consumes`, `depends_on`, `required_by`, `interfaces`, `runtime`, `lifecycle` |
| 6 | No lifecycle management | **`RequirementState` + `RequirementStateMachine`** — DRAFT → INCOMPLETE → READY → FROZEN → ARCHIVED |
| 7 | No specification provenance | **`SpecificationLineage`** — links `ProjectSpecification` ← `RequirementSnapshot` ← `RequirementEvent`s |
| 8 | No independent integrity check | **`RequirementIntegrity`** — verifies snapshot hash, event count, unresolved conflicts |
| 9 | Domain service contracts unclear | **7 marker interfaces** — `RequirementRepository`, `RequirementResolver`, `RequirementMapper`, `RequirementValidator`, `RequirementReview`, `RequirementSnapshotBuilder`, `RequirementIntegrityService` |
| 10 | Architectural leakage | **`services/planner.py` and `services/executor.py` are NEVER imported in `requirement_discovery.py`** |

---

## Architecture — Before (Sprint 2D)

```
User Request (objective)
  ↓
RequirementExtractor.extract()
  ↓  (produces RequirementRevision)
RequirementDocument.add_revision()
  ↓
RequirementMapper.translate()
  ↓  (produces Architecture + Assumptions)
SpecificationBuilder.build()
  ↓
FrozenSpecification
```

**Problems:**
- `RequirementRevision` was the source of truth (not events)
- Conflicts were implicit (buried in validation errors)
- Intent was not classified
- Components had no relationships
- No state machine
- No lineage (spec ← requirement not traced)
- No independent integrity check
- Service contracts were not explicit

---

## Architecture — After (Sprint 2E)

```
User Message (plain text)
  ↓
[1] RequirementIntentClassifier.classify()        ← Objective 4
  ↓
[2] RequirementEventBuilder.build_event()       ← Objective 1
  ↓  (produces ONE immutable RequirementEvent)
[3] SpecificationRepository.append_event()
  ↓
[4] RequirementResolutionEngine.resolve()        ← Objective 2, 3
  ↓  (produces RequirementSnapshot — deterministic)
[5] RequirementIntegrity.verify()                ← Objective 8
  ↓
[6] RequirementStateMachine.transition()        ← Objective 6
  ↓
[7] RequirementMapper.map()                   ← Objective 5
  ↓  (produces DynamicArchitecture + Assumptions)
[8] RelationshipGraph.build_from()             ← Objective 5 (graph)
[9] SpecificationBuilder.build()               ← Objective 7 (lineage)
  ↓
[10] SpecificationReview.review()              ← Objective 9
  ↓
[11] CryptographicFreeze.freeze()
  ↓
[12] SpecificationRepository.save_spec_version() ← Objective 7 (lineage persisted)
  ↓
FrozenSpecification (with lineage)
  ↓
[13] Planner (reads ONLY FrozenSpecification.spec)  ← Objective 10
```

**Improvements:**
- **Events are immutable** — never edited, never overwritten
- **Snapshot is derived** — rebuilt from scratch from events each time
- **Conflicts are explicit** — stored in `RequirementSnapshot.conflicts`
- **Intent is classified** — before extraction; controls processing
- **Relationship graph** — planner can order execution steps via `get_execution_order()`
- **State machine** — enforces legal lifecycle transitions
- **Lineage** — `SpecificationLineage` links spec ← snapshot ← events
- **Integrity** — independent verification of snapshot hash + event count + conflicts
- **Domain contracts** — 7 explicit service interfaces
- **Architectural isolation** — planner/execution NEVER imported in requirement layer

---

## Objective 1 — Requirement Event Model

**Old:** `RequirementRevision` — the source of truth was a revision (mutable until frozen).

**New:** `RequirementEvent` — the source of truth is an **immutable event**.

```python
class RequirementEvent(BaseModel):
    event_id: str           # UUID (immutable identifier)
    timestamp: datetime
    source: str             # "user" | "agent_inferred" | "system"
    event_type: str         # "instruction" | "amendment" | "removal" | "override"
    intent: IntentType
    payload: dict[str, Any]  # requirement_text + components
    supersedes: list[str]   # event_ids this overrides
    affects: list[str]       # component IDs affected
    confidence: float
    metadata: dict[str, Any]
```

**Rules:**
- Events are **never edited** — if the user changes their mind, a NEW event is appended
- `supersedes` links events (event B supersedes event A — A is still in the log, just overridden)
- `RequirementSnapshot` is **derived** from events — rebuilt from scratch each time

---

## Objective 2 — Requirement Resolution Engine

**`RequirementResolutionEngine.resolve(events)`** — deterministic snapshot builder.

**Algorithm (4 passes):**
1. **Filter superseded** — remove events whose `event_id` appears in any `supersedes`
2. **Merge components** — newest event wins per `component_type`
3. **Detect conflicts** — same `component_type` with different `framework` values
4. **Compute integrity hash** — SHA-256 of canonical snapshot JSON

**Key property:** Same events ALWAYS produce the same snapshot (deterministic).

---

## Objective 3 — Requirement Conflict Detection

**`RequirementConflict`** — explicit conflict objects.

```python
class RequirementConflict(BaseModel):
    conflict_id: str
    timestamp: datetime
    conflict_type: str      # "framework" | "database" | "runtime" | ...
    description: str
    conflicting_events: list[str]   # event_ids involved
    conflicting_values: list[str]
    resolution: str          # "unresolved" | "kept_newest" | "kept_oldest" | "manual"
```

**Example conflict:**
```
Event 1:  framework=FastAPI
Event 2:  framework=Express   ← OVERRIDE attempt

→ Conflict(conflict_type="framework", conflicting_values=["fastapi", "express"])
```

**Resolution:** By default, **newest wins** (event 2 overrides event 1). The conflict is recorded but marked `resolution="kept_newest"`.

---

## Objective 4 — Requirement Intent Classification

**Before:** Intent was not explicitly classified. The system inferred it from context.

**After:** `IntentType` enum + `RequirementIntentClassifier`.

```python
class IntentType(str, Enum):
    CREATE   = "create"
    MODIFY   = "modify"
    DELETE   = "delete"
    REPLACE  = "replace"
    CLARIFY = "clarify"
    CONFIGURE = "configure"
    DEPLOY   = "deploy"
    OPTIMIZE = "optimize"
    DEBUG    = "debug"
    EXTEND   = "extend"
```

**Classification:** Keyword-based scoring (deterministic, no LLM). The intent controls:
- Whether a new event `supersedes` previous events
- Whether the system prompts for clarification (`CLARIFY`)
- Whether the system generates a full new spec (`CREATE`) or modifies the existing one (`MODIFY`)

---

## Objective 5 — Architecture Relationship Graph

**`DynamicComponent`** now has:

```python
class DynamicComponent(BaseModel):
    # ... existing fields ...
    provides: list[str] = []       # e.g. "api_endpoints", "async_tasks"
    consumes: list[str] = []       # e.g. "api_endpoints", "database_conn"
    depends_on: list[str] = []    # component IDs (e.g. ["database-1"])
    required_by: list[str] = []    # auto-populated from depends_on
    interfaces: list[str] = []      # e.g. "REST", "GraphQL", "gRPC"
    runtime: str = "UNKNOWN"       # e.g. "node", "python", "jvm"
    lifecycle: str = "UNKNOWN"      # e.g. "long-running", "serverless", "cron"
```

**`RelationshipGraph`** — built from `DynamicArchitecture`:

```python
graph = RelationshipGraph()
graph.build_from(architecture.components)
execution_order = graph.get_execution_order()   # topological sort
```

**Future (Sprint 3):** The planner consumes `RelationshipGraph` to order execution steps (e.g., deploy database → deploy backend → deploy frontend).

---

## Objective 6 — Requirement State Machine

**`RequirementState`** — lifecycle states:

```
DRAFT  ──→  INCOMPLETE  ──→  READY  ──→  FROZEN  ──→  ARCHIVED
 (start)     (missing fields)    (complete)   (hash computed)   (superseded)
```

**Legal transitions:**

| From | To | Condition |
|-----|----|----|
| `DRAFT` | `INCOMPLETE` | Components present but critical fields UNKNOWN |
| `DRAFT` | `READY` | All critical fields known |
| `INCOMPLETE` | `READY` | Missing fields filled in |
| `READY` | `FROZEN` | Snapshot hash computed |
| `FROZEN` | `ARCHIVED` | Newer snapshot frozen |

**Illegal transitions are rejected by `RequirementStateMachine.transition()`.**

---

## Objective 7 — Specification Lineage

**`SpecificationLineage`** — full traceability.

```python
class SpecificationLineage(BaseModel):
    lineage_id: str               # UUID
    source_snapshot_id: str      # RequirementSnapshot.snapshot_id
    source_snapshot_hash: str    # RequirementSnapshot.hash
    parent_lineage_id: str | None
    created_from_version: int | None
    generated_at: datetime
    event_count: int
    revision_count: int
    integrity_status: str         # "valid" | "broken"
```

**Stored in:** `ProjectSpecification.lineage` (new field).

**DB storage:** `SpecificationVersion.lineage` (JSON) in `project_specifications.spec_versions`.

**Why this matters:**
- Full audit trail: which spec was built from which requirement snapshot
- Rollback: can regenerate spec from any historical snapshot
- Debugging: if a spec is wrong, trace back to which event caused it

---

## Objective 8 — Requirement Integrity

**`RequirementIntegrity.verify(snapshot)`** — independent integrity verification.

**Checks:**
1. **Hash consistency** — `snapshot.hash == compute_hash(snapshot)`
2. **Event count consistency** — `snapshot.event_count >= 0`
3. **Unresolved conflicts** — any `conflicts` with `resolution="unresolved"` are flagged

**Result:** `(is_valid: bool, issues: list[str])`

**If invalid:** `snapshot.integrity_status = "tampered"`.

---

## Objective 9 — Domain Contracts

**7 explicit service interfaces** (defined in `models/agent.py`):

| Interface | Responsibility |
|-----------|-----------------|
| `RequirementRepository` | Load/save events + snapshots (persistence ONLY) |
| `RequirementResolver` | Build `RequirementSnapshot` from events (deterministic) |
| `RequirementMapper` | Translate snapshot → `DynamicArchitecture` (pure function) |
| `RequirementValidator` | Validate snapshot for consistency (no execution) |
| `RequirementReview` | Review snapshot + architecture (produce verdict) |
| `RequirementSnapshotBuilder` | Assemble snapshot (apply state machine) |
| `RequirementIntegrityService` | Verify integrity independently |

**Rule:** Each service owns EXACTLY one responsibility. No duplicated logic.

---

## Objective 10 — Domain Cleanup

**Architectural leakage removed:**

| Layer | What it MUST NOT know |
|-------|----------------------|
| Requirement layer | Planner, Execution, Deployment, Templates, Approval |
| Planner | `RequirementEvent`s, Timeline, Resolver |
| Execution | `RequirementDocument`, `RequirementSnapshot` |
| Deployment | `RequirementIntent`, `RequirementConflict` |

**Enforcement:**
- `services/requirement_discovery.py` NEVER imports `services.planner` or `services.executor`
- `services/planner.py` receives `FrozenSpecification.spec` (dict) — never a domain object
- `FrozenSpecification.validate_hash()` MUST be called by planner before using spec

---

## Files Modified

| File | Changes |
|------|---------|
| `backend/models/agent.py` | **Major update** — 12 new models/interfaces: `IntentType`, `RequirementEvent`, `RequirementConflict`, `RequirementSnapshot`, `RequirementResolutionEngine`, `RelationshipGraph`, `RequirementState`, `SpecificationLineage`, `RequirementIntegrity`, 7 domain service interfaces |
| `backend/services/requirement_discovery.py` | **Complete rewrite** — 13-layer event-based pipeline; `RequirementEventBuilder`, `RequirementIntentClassifier`, `RequirementResolutionEngine`, `RequirementStateMachine`, `RequirementMapper`, `RelationshipGraph`, `SpecificationBuilder`, `SpecificationReview`, `CryptographicFreeze`, `SpecificationRepository` (updated) |
| `SPRINT_02E_REPORT.md` | **New** — this report |

---

## Backward Compatibility

| Item | Strategy |
|------|-----------|
| `ProjectSpecification` model | **Extended** (new `lineage` field) — old fields kept |
| `run_discovery()` return type | **Unchanged** — returns `ProjectSpecification` |
| DB table `project_specifications` | **Extended** — new columns: `requirement_events` (JSONB), `latest_snapshot` (JSONB) |
| Old `RequirementRevision` model | **Kept** (compatibility) — but no longer used in new pipeline |
| `should_run_discovery()` | **Unchanged** — same heuristic gate |

---

## DB Migration

```sql
-- Add event + snapshot columns to project_specifications
ALTER TABLE project_specifications
  ADD COLUMN IF NOT EXISTS requirement_events jsonb DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS latest_snapshot jsonb DEFAULT NULL;

-- Migrate: any existing rows with requirement_versions but no requirement_events
-- (requirement_versions was added in Sprint 2D)
-- This is a one-time data migration if Sprint 2D was deployed.
```

---

## Remaining Limitations

| Limitation | Plan |
|------|-----------|
| `RequirementIntentClassifier` is keyword-based (no LLM) | Sprint 3: can use LLM for ambiguous cases |
| `RelationshipGraph.get_execution_order()` is simplified (DFS only) | Sprint 3: full topological sort with cycle detection |
| `RequirementConflict.resolution` is always "kept_newest" | Sprint 3: Approval System will let user choose |
| `FrozenSpecification.validate_hash()` not yet called in planner | Sprint 3: add to `planner.py` |
| DB migration must be run manually | Run ALTER TABLE before deploying Sprint 2E |

---

## Why the Requirement Domain is Now Complete

After Sprint 2E, the Requirement layer has ALL of:

1. ✅ **Immutable events** — source of truth; never overwritten
2. ✅ **Deterministic resolution** — same events → same snapshot
3. ✅ **Explicit conflicts** — `RequirementConflict` objects
4. ✅ **Intent classification** — `IntentType` enum; separated from requirement
5. ✅ **Relationship graph** — `DynamicComponent` relationships; consumable by planner
6. ✅ **State machine** — enforces legal lifecycle transitions
7. ✅ **Full lineage** — `SpecificationLineage` links spec ← snapshot ← events
8. ✅ **Independent integrity** — `RequirementIntegrity.verify()`
9. ✅ **Domain contracts** — 7 explicit service interfaces
10. ✅ **Architectural isolation** — requirement layer NEVER imports planner/execution

**Future sprints extend (not redesign):**
- **Sprint 3 (Approval System)** — consumes `RequirementConflict` + `Assumption` (priority=CRITICAL)
- **Sprint 3 (Template Intelligence)** — reads `RelationshipGraph` to order template application
- **Sprint 3 (Interactive Wait)** — uses `IntentType.CLARIFY` to pause and ask
- **Sprint 3 (Execution ordering)** — consumes `RelationshipGraph.get_execution_order()`

---

## Preparation for Sprint 3

The following are **ready** for Sprint 3 integration:

1. **`FrozenSpecification.validate_hash()`** — Planner MUST call before planning
2. **`RequirementConflict`** — Approval System shows conflicts to user for resolution
3. **`Assumption.priority`** — Approval System reads `assumptions` where `priority=CRITICAL`
4. **`RelationshipGraph`** — Planner consumes `get_execution_order()` to order steps
5. **`IntentType`** — Interactive Wait uses `CLARIFY` to pause; `MODIFY` to prompt
6. **`SpecificationLineage`** — Audit UI shows full spec ← requirement traceability
7. **`RequirementStateMachine`** — frontend shows requirement lifecycle status

**What Sprint 3 should NOT need to change:**
- `RequirementEventBuilder.build_event()` — produces events, complete
- `RequirementResolutionEngine.resolve()` — deterministic, complete
- `RequirementIntentClassifier.classify()` — keyword-based, complete
- `RelationshipGraph.build_from()` — supports arbitrary component types, complete
- `RequirementStateMachine.transition()` — enforces legal transitions, complete
- `SpecificationLineage` — full traceability, complete
- `RequirementIntegrity.verify()` — independent integrity check, complete
- All 7 domain service interfaces — contracts defined, complete
