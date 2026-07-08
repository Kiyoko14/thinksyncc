# ThinkSync — Sprint 2C: Requirement Foundation Finalization

**Date:** 2026-07-04
**Goal:** Finalize the Requirement Discovery architecture. After this sprint the Requirement layer is frozen — future sprints extend it without redesigning.

---

## Sprint Goal

Implement all 9 objectives to make the Requirement Discovery layer **production-grade, versioned, architecture-aware, assumption-explicit, UNKNOWN-classified, cryptographically frozen, diffable, and architecturally validated**.

---

## Problems Solved

| # | Problem (Sprint 2B) | Fix (Sprint 2C) |
|---|---|---|
| 1 | Single `framework: str` field | **`Architecture` model** — multiple components (frontend, backend, database, cache, queue, proxy, auth, deployment, monitoring, CI/CD) |
| 2 | Spec was mutable — no version history | **`SpecificationVersion`** — immutable versions with `parent_version`, `changed_fields`, `change_reason` |
| 3 | Assumptions were implicit (invented values) | **`AssumptionEngine`** — explicit `Assumption` objects with `field`, `value`, `reason`, `confidence`, `approval_required` |
| 4 | Single `UNKNOWN` string | **`UnknownType` enum** — `REQUIRED` / `OPTIONAL` / `EXTERNAL` / `DEFERRED` |
| 5 | `FrozenSpecification` had no tamper detection | **`CryptographicFreeze`** — SHA-256 of canonical JSON; `validate_hash()` must be called by downstream |
| 6 | Repository stored only latest spec | **`SpecificationRepository` (improved)** — full version history, version lookup, diff, rollback |
| 7 | No way to compare spec versions | **`SpecDiff.diff()`** — returns `added`, `removed`, `changed`, `unchanged` |
| 8 | Downstream could re-interpret user request | **`FrozenSpecification` enforced** — `.spec` is the ONLY source of truth after freeze |
| 9 | No validation of complete architecture | **`ArchitectureValidator`** — validates frontend+backend+db+cache+queue+auth+deployment together |

---

## Architecture Before (Sprint 2B)

```
User Request
  ↓
[1] RequirementExtractor.extract()        ← LLM-first (temp=0.0)
  ↓
[2] RequirementValidator.validate()        ← framework/language compat
  ↓
[3] QuestionPlanner.plan_questions()    ← Critical/Required/Optional
  ↓
[4] SpecificationBuilder.build()          ← assembles spec dict
  ↓
[5] SpecificationReview.review()          ← issues/warnings/recommendations
  ↓
[6] SpecificationFreeze.freeze()         ← FrozenSpecification (no hash)
  ↓
Planner (reads .spec)
```

**Problems:**
- `framework: str` — cannot represent multi-component projects (Next.js + FastAPI)
- No version history — user change overwrites previous spec
- Assumptions are implicit — agent silently assumes Python for FastAPI
- `UNKNOWN` is a single string — planner can't distinguish "can't continue" from "optional"
- `FrozenSpecification` has no hash — tampering undetectable
- Repository stores only latest — no rollback, no diff
- No architecture validation — e.g., Redis selected without queue usage undetected

---

## Architecture After (Sprint 2C)

```
User Request (objective + history)
  ↓
[1] RequirementExtractor.extract()        ← NOW produces Architecture + Assumptions
  ↓
[2] RequirementValidator.validate(arch)  ← framework/language compat (unchanged)
  ↓
[3] UNKNOWNClassifier.classify(arch)    ← NEW: classifies each UNKNOWN
  ↓                                      ← NEW: generates Assumption objects
[4] QuestionPlanner.plan_questions()    ← now uses architecture components
  ↓
[5] SpecificationBuilder.build()          ← now includes architecture + assumptions
  ↓
[6] SpecificationReview.review()          ← NOW also runs ArchitectureValidator
  ↓
[7] CryptographicFreeze.freeze()         ← NEW: SHA-256 hash of canonical JSON
  ↓
[8] SpecificationRepository.save_version()← NEW: saves ALL versions in Postgres
  ↓
FrozenSpecification (with hash)
  ↓
Planner (MUST call .validate_hash() before using)
```

**Improvements:**
- `Architecture` model — multi-component, each with independent `framework` / `language` / `unknown_type`
- Versioning — every meaningful change creates a new `SpecificationVersion` (immutable)
- Explicit assumptions — `AssumptionEngine` generates `Assumption` objects; future Approval System consumes them
- `UnknownType` enum — planner can decide per-tier (Critical blocks, Optional continues)
- Cryptographic freeze — `FrozenSpecification.validate_hash()` detects tampering
- Repository — full version history, `diff()`, `rollback_to()`
- `ArchitectureValidator` — understands complete project architecture

---

## Specification Versioning (Objective 1)

**Model:** `SpecificationVersion` (in `models/agent.py`)

```python
class SpecificationVersion(BaseModel):
    version: int = 1
    spec_json: dict[str, Any]
    frozen_at: datetime | None
    frozen_hash: str          # SHA-256 (Objective 5)
    parent_version: int | None
    change_reason: str
    changed_fields: list[str]
    assumptions: list[Assumption]
    review_verdict: str       # "pass" | "pass_with_warnings" | "fail"
```

**Rules:**
- Versions are **immutable** once frozen
- `parent_version` links to previous version
- `changed_fields` lists what changed (used by `SpecDiff`)
- `change_reason` is mandatory on explicit user-driven changes
- Never overwrite previous specifications

**Storage:** `project_specifications.spec_versions` (JSONB array in Postgres)

---

## Architecture-aware Specification (Objective 2)

**Old model:**
```python
framework: str = "unknown"   # single framework
language: str = "unknown"    # single language
```

**New model:**
```python
architecture: Architecture = Field(default_factory=Architecture)

class Architecture:
    components: list[ArchitectureComponent]

class ArchitectureComponent:
    component: str              # "frontend", "backend", "database", ...
    framework: str = "UNKNOWN" # "next.js", "fastapi", ...
    language: str = "UNKNOWN"  # "python", "javascript", ...
    version_constraint: str = "UNKNOWN"
    config: dict[str, Any]
    unknown_type: UnknownType | None
    notes: str = ""
```

**Example — multi-framework project:**
```python
Architecture(
    components=[
        ArchitectureComponent(component="frontend", framework="Next.js", language="typescript"),
        ArchitectureComponent(component="backend", framework="FastAPI", language="python"),
        ArchitectureComponent(component="database", framework="PostgreSQL"),
        ArchitectureComponent(component="cache", framework="Redis"),
        ArchitectureComponent(component="queue", framework="Celery"),
    ]
)
```

**Backward compatibility:** Deprecated scalar fields (`framework`, `language`) are kept and auto-synced from `architecture` in `ProjectSpecification.from_architecture()`.

---

## Assumption Engine (Objective 3)

**Problem:** The agent previously silently assumed things (Python for FastAPI, SQLite if no DB specified). These silent assumptions are dangerous — the user may have meant something else.

**Solution:** `AssumptionEngine` generates explicit `Assumption` objects.

```python
class Assumption(BaseModel):
    field: str               # "backend.language"
    value: str               # "python"
    reason: str              # "FastAPI requires Python"
    confidence: float        # 0.0–1.0
    can_be_confirmed: bool   # can the user correct this?
    approval_required: bool  # should the Approval System ask the user?
    created_at: datetime
```

**Where assumptions are generated:**
1. `RequirementExtractor.extract()` — LLM returns `assumptions` array
2. `AssumptionEngine.from_unknown_fields()` — generates assumptions for UNKNOWN fields (with `approval_required=True`)

**Future:** The Approval System will consume `Assumption` objects and ask the user for confirmation.

---

## UNKNOWN Classification (Objective 4)

**Old:** `framework = "unknown"` — planner can't decide what to do.

**New:** Every UNKNOWN is classified:

```python
class UnknownType(str, Enum):
    REQUIRED   = "unknown_required"    # Planning CANNOT continue
    OPTIONAL   = "unknown_optional"    # Planning continues
    EXTERNAL  = "unknown_external"   # Waiting for external resource
    DEFERRED  = "unknown_deferred"    # Needed only in later phases
```

**Classification rules** (in `UNKNOWNClassifier.classify()`):

| Field | Classification | Reason |
|---|---|---|
| `backend.framework` = UNKNOWN | `REQUIRED` | Cannot generate backend code |
| `frontend.framework` = UNKNOWN | `REQUIRED` | Cannot generate frontend code |
| `deployment_target` = UNKNOWN | `OPTIONAL` | Can default to localhost |
| `cache.framework` = UNKNOWN | `OPTIONAL` | Cache is optional |
| `monitoring.framework` = UNKNOWN | `DEFERRED` | Monitoring is phase-2 |

**Effect on planner:**
- `readiness="Blocked"` if any `REQUIRED` is UNKNOWN
- `readiness="Partial"` if only `OPTIONAL` / `DEFERRED` is UNKNOWN
- `readiness="Ready"` if no `REQUIRED` is UNKNOWN

---

## Cryptographic Freeze (Objective 5)

**Problem:** `FrozenSpecification` in Sprint 2B was a wrapper but had no tamper detection. A bug in downstream code could modify the spec dict and go undetected.

**Solution:** `CryptographicFreeze.freeze()` produces a `FrozenSpecification` with:

1. **Canonical JSON** — `json.dumps(spec, sort_keys=True, separators=(",",":"))`
2. **SHA-256 hash** — `hashlib.sha256(canonical.encode()).hexdigest()`
3. **`validate_hash()`** — downstream MUST call this before using the spec

```python
class FrozenSpecification(BaseModel):
    spec: dict[str, Any]
    frozen_at: datetime
    frozen_hash: str        # SHA-256 of canonical JSON at freeze time
    version: int = 1
    is_tampered: bool = False

    def validate_hash(self) -> bool:
        return self.compute_hash() == self.frozen_hash

    def mark_tampered(self) -> None:
        self.is_tampered = True
```

**Downstream contract:** Planner, Execution, Patch, Verification MUST:
1. Read `FrozenSpecification.spec`
2. Call `validate_hash()` — if `False`, log error and refuse to proceed

---

## Specification Repository (Objective 6)

**Old:** `SpecificationRepository` stored only the latest spec in `spec_json`.

**New:** Stores ALL versions in `spec_versions` (JSONB array).

**New capabilities:**
```python
# Version history
await SpecificationRepository.get_latest(conv_id)       # → (spec, review)
await SpecificationRepository.get_version(conv_id, 3)    # → SpecificationVersion

# Diff
await SpecificationRepository.diff_versions(conv_id, 1, 3)  # → SpecDiff result

# Rollback
await SpecificationRepository.rollback_to(conv_id, 2)  # → creates v4 copied from v2
```

**Postgres schema:**
```sql
CREATE TABLE project_specifications (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   text NOT NULL UNIQUE,
    user_id           text NOT NULL DEFAULT '',
    spec_versions     jsonb NOT NULL DEFAULT '[]'::jsonb,   -- ALL versions
    latest_version    int NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
```

---

## Specification Diff (Objective 7)

**`SpecDiff.diff(old_spec, new_spec)`** returns:

```python
{
    "added":    ["production_ready", "architecture.components[proxy]"],
    "removed":  ["external_services[old_api]"],
    "changed":  [
        {"field": "architecture.components[backend].framework", "old": "flask", "new": "fastapi"},
        {"field": "deployment_target", "old": "localhost", "new": "cloud"},
    ],
    "unchanged": ["project_type", "auth_required"],
}
```

**Future use cases:**
- **Approval System** — show the user exactly what changed between versions
- **Audit** — full project evolution timeline
- **Rollback** — `changed_fields` tells you what will be reverted
- **Project Timeline** — visual diff in frontend

---

## Architecture Validation (Objective 9)

**`ArchitectureValidator.validate(arch, spec)`** checks complete project architecture:

| Check | Type |
|---|---|
| Backend without language | **ERROR** (can't generate code) |
| `production_ready=True` without deployment runtime | **ERROR** (can't generate deployment config) |
| Database selected without driver/ORM | **WARNING** |
| Cache selected without session/queue use case | **WARNING** |
| `auth_required=True` without `auth` component | **WARNING** |
| Frontend without backend | **WARNING** (JAMstack is valid, but confirm intent) |

**Integrated into `SpecificationReview`:** The review stage now runs BOTH:
1. Compatibility validation (`RequirementValidator`)
2. Architecture validation (`ArchitectureValidator`)

---

## Files Modified

| File | Changes |
|------|---------|
| `backend/models/agent.py` | **Major update** — added `UnknownType`, `Assumption`, `ArchitectureComponent`, `Architecture`, `SpecificationVersion`, `FrozenSpecification` (updated), `ProjectSpecification` (updated with `architecture`, `assumptions`, `unknown_fields`, `versions`, `frozen_spec`) |
| `backend/services/requirement_discovery.py` | **Major update** — added `RequirementExtractor` (architecture-aware), `AssumptionEngine`, `UNKNOWNClassifier`, `ArchitectureValidator`, `CryptographicFreeze`, updated `SpecificationBuilder`, `SpecificationReview`, `FrozenSpecification`, `SpecificationRepository` (versioned), `SpecDiff` |
| `SPRINT_02C_MIGRATION.sql` | **New** — migrates `project_specifications` table to versioned format |

---

## Backward Compatibility

| Item | Strategy |
|------|-----------|
| `ProjectSpecification.framework` (scalar) | **Deprecated** but kept; auto-synced from `architecture` |
| `ProjectSpecification.language` (scalar) | **Deprecated** but kept; auto-synced from `architecture` |
| `run_discovery()` return type | **Unchanged** — returns `ProjectSpecification` |
| `should_run_discovery()` | **Unchanged** — same heuristic gate |
| `get_cached_spec()` | **Updated** — reads from `SpecificationRepository.get_latest()` |
| Postgres `spec_json` column | **Kept** — older rows still readable; new rows also populate `spec_versions` |

---

## Remaining Limitations

| Limitation | Plan |
|---|---|
| `FrozenSpecification.validate_hash()` is not yet called by Planner/Executor | Sprint 3: add hash validation to all downstream consumers |
| `Assumption.approval_required` is set but not consumed | Sprint 3: Approval System will read `assumptions` and prompt user |
| `ArchitectureValidator` rules are conservative (many warnings) | Sprint 3: tune rules based on real project patterns |
| DB migration (`SPRINT_02C_MIGRATION.sql`) must be run manually | Run before deploying Sprint 2C |
| `conversation_id` may still be `None` from client | Sprint 3: make `conversation_id` required in `JobCreate` |

---

## Why Requirement Discovery is Now Considered Complete

After Sprint 2C, the Requirement Discovery layer has:

1. ✅ **Deterministic extraction** — LLM-first (`temp=0.0`), regex fallback only
2. ✅ **Versioning** — immutable `SpecificationVersion` with full history
3. ✅ **Architecture-aware** — multi-component, each independently specified
4. ✅ **Explicit assumptions** — no silent guessing
5. ✅ **UNKNOWN classification** — 4-tier system for planner decision-making
6. ✅ **Cryptographic freeze** — tamper detection via SHA-256
7. ✅ **Diffable** — `SpecDiff` between any two versions
8. ✅ **Rollback-capable** — `SpecificationRepository.rollback_to()`
9. ✅ **Architecture-validated** — complete project architecture validation
10. ✅ **Clean interfaces** — `RequirementExtractor`, `AssumptionEngine`, `UNKNOWNClassifier`, `ArchitectureValidator`, `CryptographicFreeze`, `SpecificationRepository`, `SpecDiff`

**Future sprints extend (not redesign):**
- **Sprint 3 (Approval System)** — consumes `Assumption` objects, calls `SpecDiff` for user confirmation
- **Sprint 3 (Interactive Wait)** — uses `QuestionPlanner` output, pauses job, resumes after user reply
- **Sprint 3 (Template Intelligence)** — reads `Architecture` to select templates

---

## Preparation for Sprint 3

The following are **ready** for Sprint 3 integration:

1. **`FrozenSpecification.validate_hash()`** — Planner MUST call this before planning
2. **`Assumption` objects** — Approval System reads `assumptions` where `approval_required=True`
3. **`SpecDiff.diff()`** — Approval System shows the user what changed
4. **`SpecificationRepository.rollback_to()`** — frontend "Undo" button
5. **`Architecture` model** — Template Intelligence reads components to select templates
6. **`UnknownType` enum** — Interactive Wait uses priority tiers to decide which questions to ask
7. **`QuestionPlanner.plan_questions()`** — returns structured questions; frontend renders them

**What Sprint 3 should NOT need to change:**
- `RequirementExtractor.extract()` — architecture-aware, complete
- `RequirementValidator.validate()` — comprehensive
- `UNKNOWNClassifier.classify()` — 4-tier classification, complete
- `ArchitectureValidator.validate()` — complete project architecture checks
- `CryptographicFreeze.freeze()` — tamper-aware, complete
- `SpecificationRepository` — versioned, diffable, rollback-capable
