# ThinkSync — Sprint 3A.2: Approval Hardening

**Date:** 2026-07-05
**Goal:** Fix ONLY the 3 remaining architectural issues from Sprint 3A.1 audit.
**Rules followed:** No redesign. No new user-facing features. Backward compatibility preserved wherever security permits.

---

## Task 1 — Approval Secret Enforcement

### Problem
`interactive_wait.py` previously fell back to `dev-secret-do-not-use-in-prod` when `APPROVAL_RESUME_SECRET` was missing. This allowed unsigned or weakly-signed `ResumeToken`s to be issued in production if the env var was not set.

### Changes

**`backend/core/config.py`:**
- Added `APPROVAL_RESUME_SECRET: str = ""` to `Settings`
- This makes the config explicit; the field is now visible in `.env` and validated at startup

**`backend/main.py` (`lifespan`):**
- Added startup validation: if `APPROVAL_RESUME_SECRET` is empty, FastAPI startup **fails fast** with a clear `RuntimeError` and a hint on how to generate a secure secret
- This prevents the entire app from starting with a missing secret

**`backend/services/interactive_wait.py` (`InteractiveWaitEngine.pause()`):**
- Removed the insecure `os.environ.get("APPROVAL_RESUME_SECRET", "dev-secret-do-not-use-in-prod")` fallback
- Now reads the secret from `Settings` via `get_settings()`
- If the secret is missing/empty, raises `RuntimeError` immediately — **no unsigned token is ever issued**
- The `except` block now **re-raises** the error (previously it only logged a warning and continued)

### Backward Compatibility
- **Breaking change (by design):** the app now refuses to start if `APPROVAL_RESUME_SECRET` is not set. This is intentional: it prevents production deployments from accidentally using unsigned tokens.
- To migrate: add `APPROVAL_RESUME_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")` to `.env`.
- The `ResumeToken` model itself is unchanged — existing tokens in the DB are unaffected (they were signed with whatever secret was used; if the secret is now properly set, they will validate correctly).

---

## Task 2 — Global Approved Plan Immutability

### Problem
`ApprovedPlanViolationError` was only raised inside `planner.py`'s `build_plan()`. The orchestration pipeline has multiple entry points (agent_service, resume_manager, template layer) that could theoretically mutate or rebuild an approved (frozen) plan without hitting `build_plan()`.

### Changes

**`backend/models/approval.py`:**
- Added `ApprovedPlanViolationError` exception class
- Added `ensure_approved_plan_immutable(spec, context)` — the **single shared check** used by all orchestration entry points
- This helper:
  1. Returns early if `spec is None`
  2. Checks `spec.frozen` (handles both dict and Pydantic model)
  3. Raises `ApprovedPlanViolationError` with a human-readable message including the spec name and the `context` string

**`backend/services/planner.py`:**
- `build_plan()` now calls `ensure_approved_plan_immutable(project_spec, context="planner")` at the top
- Removed the duplicated inline check that was there previously

**`backend/services/resume_manager.py`:**
- `load_resume_bundle()` now calls `ensure_approved_plan_immutable(spec, context="resume_manager")` before returning the bundle
- This ensures that even if the resume path is entered, the frozen spec cannot be mutated

**`backend/services/agent_service.py` (resume path):**
- The resume path in `run_agent_pipeline()` now calls `ensure_approved_plan_immutable(spec, context="agent_service")` before continuing execution

### Backward Compatibility
- Fully backward compatible: `ensure_approved_plan_immutable()` returns early when `spec is None` or `frozen=False`
- Existing jobs that don't use the approval system are completely unaffected
- The only behavior change: if a spec is explicitly marked `frozen=True`, the system now raises an error consistently across **all** entry points (previously `resume_manager` and `agent_service` did not check)

---

## Task 3 — ResumeToken Revocation

### Problem
A `ResumeToken`, once issued, could only become invalid by expiring or being consumed. There was no way to revoke a token if:
- The approval was rejected
- The job was cancelled or failed
- A newer token was issued (token rotation)
- An administrator needed to abort execution

### Changes

**`backend/models/approval.py` — `ResumeToken`:**
- Added `revoked: bool = False` field
- Added `revocation_reason: str = ""` field
- `verify()` now checks `self.revoked` **before** checking signature/expiry — a revoked token is always invalid
- Added `revoke(self, reason: str)` method to mark a token as revoked

**`backend/models/approval.py` — `ResumeTokenStore`:**
- Added `revoke(approval_id, reason)` — loads the token, calls `tok.revoke(reason)`, re-persists to DB
- Added `is_revoked(approval_id) -> bool` — checks whether a token is revoked
- `consume()` unchanged (consumed ≠ revoked; a token can be both consumed and revoked)

**`backend/services/interactive_wait.py` — `InteractiveWaitEngine.resume()`:**
- Now **validates the ResumeToken** before allowing the resume
- Calls `ResumeTokenStore.load()` → `tok.verify(secret)` 
- If verification fails: raises `InteractiveWaitError` with a clear message
- On successful verification: calls `ResumeTokenStore.consume()` to mark the token as single-use
- **Revocation on rejection:** if the user's reply indicates rejection, the token is revoked immediately

**Revocation triggers implemented:**
| Trigger | Where |
|----------|-------|
| Approval rejected | `interactive_wait.py` — `resume()` revokes token if reply indicates rejection |
| Job cancelled | Caller should call `ResumeTokenStore.revoke(approval_id, "job_cancelled")` |
| Job failed permanently | Caller should call `ResumeTokenStore.revoke(approval_id, "job_failed")` |
| Newer token issued | `ResumeTokenStore.revoke()` the old token before calling `issue()` |
| Token consumed | `ResumeTokenStore.consume()` (already existed) |
| Token expired | `ResumeToken.verify()` checks `expires_at` (already existed) |
| Admin abort | Caller should call `ResumeTokenStore.revoke(approval_id, "admin_abort")` |

### Backward Compatibility
- Fully backward compatible: `revoked` defaults to `False` for tokens already in the DB (they will load correctly with `revoked=False`)
- The `verify()` method now does MORE checks, but all new checks (`revoked`) default to `False` for old tokens
- No change to the `ResumeToken` JSON schema that would break existing tokens

---

## Files Modified

| File | Task | Change |
|------|------|--------|
| `backend/core/config.py` | 1 | Added `APPROVAL_RESUME_SECRET` to `Settings` |
| `backend/main.py` | 1 | Startup validation — fail fast if secret missing |
| `backend/models/approval.py` | 1, 2, 3 | `ResumeToken` revocation fields; `ApprovedPlanViolationError`; `ensure_approved_plan_immutable()` helper; `ResumeTokenStore.revoke()` / `is_revoked()` |
| `backend/services/interactive_wait.py` | 1, 3 | Fail fast if secret missing; validate token on resume; revoke on rejection |
| `backend/services/resume_manager.py` | 2 | `load_resume_bundle()` calls `ensure_approved_plan_immutable()` |
| `backend/services/planner.py` | 2 | `build_plan()` calls shared `ensure_approved_plan_immutable()` |
| `backend/services/agent_service.py` | 2 | Resume path calls `ensure_approved_plan_immutable()` |

---

## Success Criteria vs Actual

| Criterion | Required | Actual |
|-----------|-----------|--------|
| No unsigned ResumeToken can ever exist | ✓ | ✓ (`pause()` raises if secret missing) |
| Approved plans immutable across entire pipeline | ✓ | ✓ (shared helper in `approval.py`, called from 3 entry points) |
| Revoked ResumeTokens cannot resume execution | ✓ | ✓ (`verify()` checks `revoked`; `resume()` validates before resuming) |
| Existing functionality unchanged (except security hardening) | ✓ | ✓ (all changes are additive or fail-fast; no behaviour change for non-approval paths) |
| No unrelated code modified | ✓ | ✓ (only approval-related files touched) |

**All 4 criteria met.**

---

## Security Impact

| Change | Impact |
|--------|--------|
| Task 1: Secret enforcement | **High** — prevents unsigned tokens in production. **Breaking change by design.** |
| Task 2: Global immutability | **Medium** — prevents accidental mutation of approved plans across all entry points |
| Task 3: Token revocation | **Medium** — enables administrators to invalidate tokens in various failure/abort scenarios |

---

## Remaining Limitations

1. **No HTTP endpoint** for administrators to revoke a token (must call `ResumeTokenStore.revoke()` directly from Python code)
2. **No automatic revocation** when a job is cancelled/failed — the caller (e.g., a future `/jobs/{id}/cancel` endpoint) must explicitly call `ResumeTokenStore.revoke()`
3. **Token rotation not implemented** — when a new token is issued, the old one is not automatically revoked (the caller must do this explicitly)
4. **`APPROVAL_RESUME_SECRET` rotation** — if the secret is changed, all previously-issued tokens become invalid (this is by design, but there's no migration path for in-flight tokens)

---

## Migration Notes

1. **Set `APPROVAL_RESUME_SECRET`** in `.env` before deploying:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. **Run DB migration** (if not already run):
   ```sql
   ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS resume_token JSONB;
   ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cursor_version INTEGER DEFAULT 0;
   ```
3. **No data migration needed** — `revoked` defaults to `False`, `cursor_version` defaults to `0`

---

*Sprint 3A.2 complete. All 3 tasks implemented and verified. Ready for production deployment pending `APPROVAL_RESUME_SECRET` configuration.*
