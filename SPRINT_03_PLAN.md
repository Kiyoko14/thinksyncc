# Sprint 3 Implementation Plan

## Current State
- `agent_service.py` (2072 lines) — main orchestration loop
- `run_agent_pipeline()` (line 1112) — main entry point
- `_run_code_execution()` (line 558) — execution loop
- `ApprovalEngine` created in `approval_engine.py` ✅
- `InteractiveWaitEngine` created in `interactive_wait.py` ✅
- `ClarificationEngine` created in `clarification_engine.py` ✅
- `ResumeManager` created in `resume_manager.py` ✅
- `ApprovalPolicyEngine` created in `approval_policy.py` ✅
- Models created: `approval.py`, `interaction.py` ✅
- DB migration written: `SPRINT_03_MIGRATION.sql` ✅

## Integration Plan (3 phases)

### Phase 1: Hook ApprovalEngine into execution loop
**File:** `agent_service.py`
**Where:** Inside `_run_code_execution()` — before each step executes
**What:**
1. Import `ApprovalPolicyEngine`
2. Before executing each tool call, call `policy_engine.pre_execute_check()`
3. If approval required → call `InteractiveWaitEngine.pause()` → update job status to `waiting_for_user`
4. Return early from `_run_code_execution()` with `status=waiting_for_user`

**Risk:** Low — additive only, doesn't change existing flow

### Phase 2: Hook InteractiveWaitEngine into job status
**File:** `agent_service.py`, `main.py` (routers)
**What:**
1. Add `waiting_for_user` status check at start of `run_agent_pipeline()`
2. If job is `waiting_for_user` → load `ExecutionCursor` → resume from `resume_point`
3. Add API endpoint `POST /jobs/{id}/resume` for user to approve/reject

**Risk:** Medium — modifies job state machine

### Phase3: Planner Integration (Objective 10)
**File:** `planner.py`
**What:**
1. Pass `ApprovalContext` to `build_plan()`
2. Planner respects `FrozenSpecification` — doesn't regenerate approved plan
3. Add `approval_summary` to LLM context

**Risk:** Low — additive to planner context

## Files to Modify
1. `backend/services/agent_service.py` — Phase 1 + 2
2. `backend/services/planner.py` — Phase 3
3. `backend/main.py` — register new routers
4. `backend/routers/approvals.py` — new router for approval APIs

## Files to Create
1. `backend/routers/approvals.py` — approval API endpoints
2. `SPRINT_03_REPORT.md` — sprint report

## Not Implementing (deferred to Sprint 4)
- Full conversation continuation ("continue", "change backend to FastAPI")
- Complex clarification session UI
- WebSocket streaming for approval requests
- Full `ApprovalPolicy` UI configuration

## Success Criteria
✅ Agent pauses safely before high-risk actions
✅ User can approve/reject via API
✅ Job resumes exactly where it stopped
✅ No repeated planning
✅ No duplicated execution
✅ Every approval is audited
✅ Planner respects frozen specification
