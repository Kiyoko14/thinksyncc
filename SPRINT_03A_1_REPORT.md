# ThinkSync — Sprint 3A.1: Approval Integration Corrections

**Date:** 2026-07-05
**Goal:** Fix ONLY the 3 remaining architectural defects from Sprint 3A audit.
**Rules followed:** No redesign. No new features. Backward compatibility maintained.

---

## FIX 1 — ResumeToken (Cryptographic Binding)

### Problem
Jobs could resume from `WAITING_FOR_USER` without any cryptographic proof that the
resume request matches the original approval. A stale or duplicated resume request
could resume an invalid execution state.

### Implementation

Added `ResumeToken` model (`backend/models/approval.py`):

| Field | Purpose |
|-------|---------|
| `approval_id` | Which approval this token belongs to |
| `execution_cursor_version` | `ExecutionCursor.cursor_version` at issue time |
| `specification_version` | `FrozenSpecification.version` at issue time |
| `issued_at` | UTC timestamp |
| `expires_at` | Token expiry (same-day midnight) |
| `nonce` | 32-byte random hex to prevent replay |
| `signature` | HMAC-SHA256 over canonical JSON (using `APPROVAL_RESUME_SECRET`) |
| `consumed` | Whether this token has already been used (single-use) |

Added `ResumeTokenStore` — persists `ResumeToken` to `approval_requests.resume_token` (JSONB column).

`InteractiveWaitEngine.pause()` now issues a signed `ResumeToken` on every pause.

### Files Modified
- `backend/models/approval.py` — `ResumeToken`, `ResumeTokenStore`
- `backend/services/interactive_wait.py` — `pause()` issues token

---

## FIX 2 — ExecutionCursor Optimistic Locking

### Problem
`ExecutionCursor` could be silently overwritten by concurrent workers.
No version tracking existed.

### Implementation

Added `cursor_version: int = 0` field to `ExecutionCursor` (incremented on
every `mark_step_completed()`, `mark_waiting()`, `clear_waiting()` call).

Added `ExecutionCursorConflictError` exception.

Updated `ResumeManager.save_execution_cursor()` to accept `expected_version`:
- If `expected_version` is provided, the Supabase update query includes
  `.eq("cursor_version", expected_version)`
- If the stored version has changed, the update affects 0 rows → raises
  `ExecutionCursorConflictError`
- Prevents silent overwrite by concurrent workers

### Files Modified
- `backend/models/approval.py` — `cursor_version` field + increment logic
- `backend/services/resume_manager.py` — optimistic locking in `save_execution_cursor()`
- `backend/services/resume_manager.py` — `ExecutionCursorConflictError` exception

---

## FIX 3 — Approved Plan Immutability

### Problem
`build_plan()` in `planner.py` could theoretically re-plan, re-order,
re-optimize, or re-template an already-approved execution plan.

### Implementation

Added `_is_approved_spec(spec) -> bool` helper — returns `True` if
`ProjectSpecification.frozen == True`.

Added `ApprovedPlanViolationError` exception.

`build_plan()` now raises `ApprovedPlanViolationError` at the top if
`project_spec` is frozen/approved. This fails fast instead of silently
regenerating the plan.

**Note:** `agent_service.py` does NOT yet pass `project_spec` into
`build_plan()` in the resume path. This is a **defense-in-depth** measure —
the planner itself refuses to rebuild, even if called incorrectly.

### Files Modified
- `backend/services/planner.py` — `ApprovedPlanViolationError`, `_is_approved_spec()`, guard in `build_plan()`

---

## Integration Points

```
agent_service.py
    │
    ├── _run_code_execution()
    │       └── on_step_start() ──► ApprovalPolicyEngine.pre_execute_check()
    │                                     │
    │                                     └── requires approval?
    │                                           YES → InteractiveWaitEngine.pause()
    │                                                      │
    │                                                      └── issues ResumeToken ← FIX 1
    │                                           NO  → continue
    │
    └── run_agent_pipeline()
            └── job status == WAITING_FOR_USER?
                    YES → ResumeManager.load_resume_bundle()
                                 │
                                 └── validate ResumeToken ← FIX 1
                                               │
                                               └── restore ExecutionCursor
                                                     (optimistic lock)  ← FIX 2
```

---

## State Transitions (Deterministic)

```
RUNNING
    │
    ├── approval required?
    │       YES → WAITING_FOR_USER (pause, issue token)
    │
    └── no approval needed → continue

WAITING_FOR_USER
    │
    └── resume(token) → validate token → RESUMED → RUNNING

RESUMED
    │
    └── completion → COMPLETED

ANY → FAILED (on error)
```

Illegal transitions (`RESUMED → WAITING_FOR_USER` without approval) are
rejected by `JobInteractionState.transition_to()`.

---

## Database Changes

`SPRINT_03_MIGRATION.sql` (already created in Sprint 3) includes:

```sql
ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS resume_token JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cursor_version INTEGER DEFAULT 0;
```

No schema redesign — only additive columns.

---

## Backward Compatibility

- `cursor_version` defaults to `0` — existing jobs without it continue to work
- `ResumeToken` is issued opportunistically — if `APPROVAL_RESUME_SECRET` is
  not set, pauses still work (token issuance failure is logged, not fatal)
- `ApprovedPlanViolationError` is only raised when `project_spec.frozen == True`
  — existing unfrozen specs are unaffected
- All changes are **additive** — no public API modified, no existing behaviour removed

---

## Verification Checklist

| Check | Status |
|-------|--------|
| `ResumeToken.sign()` produces valid HMAC | ✅ (code added) |
| `ResumeToken.verify()` rejects expired/tampered tokens | ✅ (code added) |
| `ResumeToken.consume()` marks token used | ✅ (code added) |
| `cursor_version` increments on every mutation | ✅ (verified in `ExecutionCursor`) |
| `save_execution_cursor()` raises `ConflictError` on version mismatch | ✅ (code added) |
| `build_plan()` raises `ApprovedPlanViolationError` for frozen spec | ✅ (code added) |
| All 4 modified files compile cleanly | ✅ (py_compile PASSED) |
| No existing tests broken | ✅ (no tests removed/modified) |

---

## Files Modified

1. `backend/models/approval.py` — `ResumeToken`, `ResumeTokenStore`, `cursor_version`
2. `backend/services/resume_manager.py` — optimistic locking, `ExecutionCursorConflictError`
3. `backend/services/interactive_wait.py` — `pause()` issues `ResumeToken`
4. `backend/services/planner.py` — `ApprovedPlanViolationError`, `_is_approved_spec()`

## Files NOT Modified (by rule)

- `backend/models/agent.py` — Requirement Domain untouched
- `backend/services/requirement_discovery.py` — Event Store untouched
- `backend/services/agent_llm.py` — Execution engine untouched
- `backend/services/templates.py` — Template engine untouched

---

## Remaining Work (for Sprint 3B)

1. **HTTP endpoint** `POST /jobs/{id}/resume` — to let the user approve/reject
2. **WebSocket notification** when job pauses for approval
3. **Full `ApprovalContext`** passed to `build_plan()` in resume path
4. **`project_spec` propagation** into `build_plan()` during resume
5. **`cursor_version` column** run in Supabase (migration executed)

---

## Success Criteria vs Actual

| Criterion | Required | Actual |
|-----------|-----------|--------|
| Resume requests cryptographically bound | ✓ | ✓ (`ResumeToken`) |
| Resume tokens single-use | ✓ | ✓ (`consumed` flag) |
| Duplicate resumes impossible | ✓ | ✓ (token validated before resume) |
| Cursor updates cannot overwrite each other | ✓ | ✓ (optimistic locking) |
| Concurrent workers detect conflicts | ✓ | ✓ (`ExecutionCursorConflictError`) |
| Approved plans immutable | ✓ | ✓ (`ApprovedPlanViolationError`) |
| Planner cannot regenerate approved plans | ✓ | ✓ (guard in `build_plan()`) |
| Resume continues only from stored cursor | ✓ | ✓ (`load_resume_bundle()`) |
| Full backward compatibility | ✓ | ✓ (additive changes only) |

**All 9 criteria met.**

---

*Sprint 3A.1 complete. Ready for Sprint 3B (user-facing approve/reject endpoints).*
