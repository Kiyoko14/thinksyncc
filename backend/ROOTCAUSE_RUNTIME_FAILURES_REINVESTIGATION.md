# RUNTIME FAILURES — STRICT ROOT-CAUSE INVESTIGATION (RE-INVESTIGATION)

**Method:** read-only source inspection + an isolated operator-precedence proof (no repo modification).
**Critical correction to the prior report:** the previous investigation (`ROOTCAUSE_RUNTIME_STARTUP_FAILURES.md`
and the follow-up fix report) concluded the repository was *correct* for `get_supabase_async()` and that
Issues 1+2 were "stale runtime, redeploy required." **That conclusion was WRONG.** The repository code is
in fact broken. Root cause: Python `await` has *lower* precedence than attribute access / call, so
`await get_supabase_async().table(...)` parses as `await (get_supabase_async().table(...))` — i.e. `.table()`
is called on the **un-awaited coroutine**. This is proven below.

---

## A. ISSUE-BY-ISSUE INVESTIGATION

### Issues 1 + 2 — `'coroutine' object has no attribute 'table'` / `coroutine was never awaited`

**Operator-precedence proof (isolated, repo-untouched):**
```
>>> await get_client().table("jobs").execute()
AttributeError: 'coroutine' object has no attribute 'table'
RuntimeWarning: coroutine 'get_client' was never awaited
```
This is byte-for-byte the production error. `await X.y()` evaluates `X.y()` FIRST (returns a coroutine),
then tries `.y` on it → fails. `await` binds LAST, not first.

**Every repository call site** of the pattern `await get_supabase_async().<builder>(` is broken at runtime
for this reason — 24 sites:

| File | Lines |
|------|-------|
| `models/approval.py` | 298, 306, 344, 361 |
| `models/agent.py` | 639, 714 |
| `models/conversation.py` | 191 |
| `services/context_engine.py` | 222, 478 |
| `services/approval_engine.py` | 361 (also `_load` at 329-333) |
| `services/conversation_reliability.py` | 191, 417 |
| `services/resume_manager.py` | 177, 229 |
| `services/repository_index.py` | 201 |
| `services/conversation_audit.py` | 83 |
| `services/interactive_wait.py` | 343, 359 |
| `services/requirement_discovery.py` | 132, 256, 317, 349, 1030, 1050 |
| `services/agent_service.py` | 1356 (resume block) |

(The `interactive_wait.py:316` form `await get_supabase_async()` is correctly awaited as a standalone
statement; the *chained* `.table(...)` at 343/359 is the broken one — same defect class as Issue 5's
neighborhood.)

**Correct forms that exist in the repo** (proof the intent was an async proxy):
- `services/conversation_reliability.py:604` → `await (await get_supabase_async()).table(table)...` (double
  await — the ONLY correct chained form).
- `executor.py:429` / `server_service.py:164` etc. → `db = await get_supabase_async()` then `db.table(...)`
  on a separate line (correct — the await completed before chaining).

The single `core/database.get_supabase_async` definition (`core/database.py:81`, `async def`) is correct in
itself; the bug is in every caller that chains `.table()/.insert()/.update()/.upsert()/.select()` directly
on the `await`-prefixed call.

**Complete execution trace (Issue 1):**
```
run_agent_pipeline (agent_service.py:1342)
  └─ resume block (1349-1382)
       result = await get_supabase_async().table("jobs").select(...).eq("id",job_id).limit(1).execute()
                └─ Python parses as: await ( get_supabase_async().table("jobs")... )
                   get_supabase_async() → coroutine (NOT awaited)
                   coroutine.table("jobs") → AttributeError: 'coroutine' object has no attribute 'table'
       → except → logger.warning("[resume] failed to check resume state: ...")
```
**Complete execution trace (Issue 2):**
```
interactive_wait.py JobInteractionState._load_state (308)
  result = await get_supabase_async().table("jobs").select("interaction_state")...execute()
           → same AttributeError on the un-awaited coroutine
  → except → logger.warning("[wait] failed to load interaction state: ...")
```
**Classification:** `Async misuse` (operator-precedence). NOT stale-runtime, NOT shadowing (only one
`get_supabase_async` definition exists), NOT a sync/async API swap.

### Issue 3 — `"ApprovalRequest" object has no field "updated_at"`

**ApprovalRequest model** (`models/approval.py:134-169`): fields are `approval_id, job_id, conversation_id,
approval_type, status, title, description, risk_level, affected_files, affected_commands,
affected_assumptions, context, created_at, resolved_at, resolved_by, request_version, decision, reason,
spec_version, requirement_version`. **No `updated_at`.**

**DB table `approval_requests`** — two sources:
- `db/schema.sql:110-137` — columns listed; **no `updated_at`**.
- `db/migrations/20260713_sprint3_finalization.sql` — **ADDS** `updated_at timestamptz not null default
  now()` to `approval_requests` (and `approval_audit`). This migration advanced the live DB schema.

**Reconstruction path:**
```
executor on_step_start hook (executor.py:586 → awaits on_step_start)
  └─ approval_engine (ApprovalEngine)
       └─ _load(approval_id)  (approval_engine.py:321)
            result = await get_supabase_async().table("approval_requests").select("*")...execute()   (329)
            return ApprovalRequest(**result.data[0])                                            (337)
                 └─ DB row contains 'updated_at' (added by migration) but ApprovalRequest model
                    has no such field → pydantic: 'ApprovalRequest' object has no field 'updated_at'
```
**Classification:** `Partial migration`. The migration added `updated_at` to the `approval_requests` table
(live DB), but the `ApprovalRequest` Pydantic model was never updated to include it. Reconstruction via
`ApprovalRequest(**row)` therefore fails on the extra `updated_at` column. Independent of Issues 1+2
(different root: model/DB schema mismatch). NOTE: on the current (unfixed) code, `_load`'s own
`await get_supabase_async().table(...)` (Issue 1 pattern) would surface the coroutine/table error *first*;
once Issue 1 is corrected, the partial-migration `updated_at` error becomes the next failure in this path.
Both are real, verified defects.

### Issue 4 — WebSocket `Cannot call "send" once a close message has been sent`

**`routers/ws.py` `job_ws()` lifecycle:**
```
job_ws(job_id, websocket)                                   (line 58)
  token check → websocket.close(code=1008); return          (61, 68)  [auth fail paths]
  await websocket.accept()                                   (71)
  try:
      completed = await _send_history(job_id, websocket)     (73)  → loops send_json
      if not completed:
          await _stream_live_events(job_id, websocket)       (75)  → loops send_json (ping/events)
  except WebSocketDisconnect: pass                           (78)
  finally:
      await websocket.close()                                (80)
```
**Mechanism:** `_stream_live_events` (line 21) has a `while True` loop that calls
`await websocket.send_json(...)` (ping on 60s timeout, or live events). When the client disconnects,
Starlette sends a close frame and the in-flight `await websocket.send_json(...)` raises
`RuntimeError: Cannot call "send" once a close message has been sent`. This is a **plain `RuntimeError`,
NOT `WebSocketDisconnect`** — so `job_ws`'s `except WebSocketDisconnect: pass` (line 78) does **NOT** catch
it. The RuntimeError propagates out of `job_ws`, surfacing as the logged failure. The `finally`
`websocket.close()` (line 80) then runs (harmless double-close is tolerated by Starlette, but the real
error is the un-caught send-after-close RuntimeError).

**Classification:** `Lifecycle bug` / `Race condition` (send-after-close). The handler only guards
`WebSocketDisconnect` and ignores the `RuntimeError` that Starlette raises when `send` is attempted after
the close frame. Incomplete disconnect handling in the WebSocket lifecycle.

---

## B. EXECUTION TRACES
(See §A for the four traces: resume→DB, interactive_wait→DB, approval _load→ApprovalRequest, ws lifecycle.)

## C. DEPENDENCY GRAPHS

**Resume subsystem**
- Owner: `services/agent_service.run_agent_pipeline` (resume block) → `core.database.get_supabase_async`.
- Authoritative `get_supabase_async`: `core/database.py:81` (single def). Consumers: agent_service,
  resume_manager, conversation_reliability, etc.

**Interactive Wait subsystem**
- Owner: `services/interactive_wait.JobInteractionState._load_state/_persist_state` →
  `core.database.get_supabase_async`.
- Depends on `core.database` (broken call site at 343/359; import fixed previously at 312).

**Approval subsystem**
- Owner: `services/approval_engine.ApprovalEngine` → `models/approval.ApprovalRequest` +
  `core.database.get_supabase_async` (`approval_requests` table).
- Authoritative `ApprovalRequest`: `models/approval.py:134`. DB contract: `db/schema.sql` + migrations
  (`20260713_sprint3_finalization.sql` adds `updated_at`). Mismatch between model and migration = Issue 3.

**WebSocket subsystem**
- Owner: `routers/ws.py:job_ws` → `services.agent_service.AgentService` (event history / subscribe) +
  `services.redis_service` (pubsub) + `core.security.decode_token`.
- Lifecycle bug isolated in `job_ws` disconnect handling (§A Issue 4).

## D. REPOSITORY EVIDENCE (file:line)
- `core/database.py:81` — `async def get_supabase_async()` (correct definition; callers misuse precedence).
- `agent_service.py:1356` — `await get_supabase_async().table("jobs")...` (broken: coroutine.table).
- `interactive_wait.py:343/359` — `await get_supabase_async().table("jobs").update(...)` (broken).
- `services/conversation_reliability.py:604` — `await (await get_supabase_async()).table(...)` (correct form).
- `models/approval.py:134-169` — `ApprovalRequest` has NO `updated_at`.
- `db/schema.sql:110-137` — `approval_requests` table (no `updated_at`).
- `db/migrations/20260713_sprint3_finalization.sql` — `updated_at timestamptz not null default now()`
  added to `approval_requests` (and `approval_audit`).
- `services/approval_engine.py:337` — `return ApprovalRequest(**result.data[0])` (reconstruction fails on
  `updated_at`).
- `routers/ws.py:58-81` — `job_ws` lifecycle; `except WebSocketDisconnect: pass` (line 78) does not catch
  the send-after-close `RuntimeError`; `finally: await websocket.close()` (line 80).

## E. VERIFIED ROOT CAUSE
1. **Issues 1+2** — `Async misuse` (Python operator precedence): `await f().attr()` calls `.attr` on the
   un-awaited coroutine. 24 broken call sites. Repository is **wrong** (correcting the prior report).
2. **Issue 3** — `Partial migration`: migration added `updated_at` to `approval_requests` DB but the
   `ApprovalRequest` model lacks it; `ApprovalRequest(**row)` raises on the extra column.
3. **Issue 4** — `Lifecycle bug` / `Race condition`: `job_ws` catches only `WebSocketDisconnect`; a
   send-after-close `RuntimeError` escapes unhandled.

## F. ROOT-CAUSE CLASSIFICATION
- Issue 1: `Async misuse` (operator-precedence).
- Issue 2: `Async misuse` (same root as Issue 1 — one defect, two symptom sites).
- Issue 3: `Partial migration` (model/DB schema drift).
- Issue 4: `Lifecycle bug` (send-after-close; incomplete disconnect handling) / `Race condition`.

## G. RELATIONSHIPS
- **Issues 1 & 2 are the SAME root cause** (operator-precedence async misuse) — not independent.
- **Issue 3 is INDEPENDENT** of 1/2: different subsystem (ApprovalRequest model vs DB), different mechanism
  (schema drift, not await). However, both touch `approval_requests` persistence. Cascade nuance: on the
  *current* code, Issue 1's await bug in `approval_engine._load` (line 329) would mask Issue 3 (the
  coroutine/table error fires before reaching `ApprovalRequest(**row)`). Once Issue 1 is fixed, Issue 3
  becomes the next failure on that path. So Issue 3 is an *independent* root but currently *sequenced after*
  Issue 1 on the approval load path.
- **Issue 4 is INDEPENDENT** of 1/2/3 (WebSocket lifecycle, no DB/await involvement).
- Net: **3 distinct root defects** across 4 symptoms:
  (a) operator-precedence async misuse [Issues 1+2]; (b) partial migration [Issue 3]; (c) WebSocket
  lifecycle/send-after-close [Issue 4].

## H. RECOMMENDED FIX ORDER ONLY (no code / no patches)
1. **Issues 1+2 (HIGHEST, blocks nearly all DB access):** correct every `await get_supabase_async().table(...)`
   chain to `(await get_supabase_async()).table(...)` (or assign to a var first). All 24 sites. This also
   unblocks Issue 3's path.
2. **Issue 3:** reconcile `ApprovalRequest` model with the migration — add the `updated_at` field (and any
   other columns the migration added to `approval_requests`/`approval_audit`) to the Pydantic model so
   `ApprovalRequest(**row)` accepts the live DB row. (Model/DB schema parity.)
3. **Issue 4:** in `job_ws`, also catch the send-after-close `RuntimeError` (or guard `send_json` against a
   closed socket) so client-disconnect-during-send does not escape as an unhandled error; ensure the
   `finally` close is idempotent.

**NOT VERIFIED:** whether the live DB has additional columns beyond `updated_at` added by migrations that
the models also lack (only `updated_at` is evidenced by the error and the migration diff). The exact set
of model fields needing parity should be confirmed against all migration files before the Issue-3 fix.

**Statement on the prior report:** the previous conclusion that "the repository is already correct for
Issues 1+2; runtime requires redeployment" is **incorrect**. The repository contains 24 broken call sites
due to operator-precedence async misuse, proven above. Redeployment alone will NOT fix them — the code
must be corrected.
