# RUNTIME STARTUP FAILURES — STRICT ROOT-CAUSE INVESTIGATION (READ-ONLY FORENSIC)

**Method:** read-only source inspection of the current repository. No fix, no patch, no config/env/SQL/
test/routing/DE/planner change, no git. Runtime logs are the only behavioral input; repository source
is the only source of truth.

**Five errors, treated independently. Verdict: four distinct root causes; Issue 2 is a SYMPTOM of
Issue 1's same mechanism, and Issue 4 is a CONSEQUENCE of Issue 3 — see §F.**

---

## A. ISSUE-BY-ISSUE ANALYSIS

### Issue 1 — `'coroutine' object has no attribute 'table'`

**Execution trace:**
```
run_agent_pipeline (services/agent_service.py:1342)
  └─ resume block (1349-1382)
       └─ await get_supabase_async().table("jobs").select(...).eq(...).execute()   (agent_service.py:1356)
[resume] failed to check resume state: 'coroutine' object has no attribute 'table'
```
(The same shape appears in `interactive_wait.py:315` and `executor.py:429` for the interaction-state load.)

**Dependency trace:** `get_supabase_async` resolves to `core/database.py:81` (async def → returns
`_AsyncClient`). `_AsyncClient.table(...)` returns `_AsyncBuilder` (database.py:72-78); `.execute()` is
the coroutine (database.py:56-57). Because `get_supabase_async()` is **awaited first** (`await
get_supabase_async().table(...)`), the chain is valid per `database.py`.

**Repository evidence:**
- `core/database.py:81` `async def get_supabase_async() -> _AsyncClient` — returns an `_AsyncClient`, NOT
  a coroutine to be `.table()`'d.
- Every live `.table(` call site uses `await get_supabase_async().table(...)` (verified: agent_service.py:1356,
  interactive_wait.py:315, context_engine.py:222/478, resume_manager.py:177/229, requirement_discovery.py:132/256/317/349/1030/1050,
  conversation_reliability.py:191/417, approval_engine.py:361, repository_index.py:201, conversation_audit.py:83,
  interactive_wait.py:343/359). All are awaited.
- **No** call site assigns `get_supabase_async()` to a variable and then calls `.table()` without await.
- **No** duplicate/redefinition of `get_supabase_async` exists in non-test source (only `core/database.py:81`
  and test mocks in `tests/test_authorization_hardening.py:96`).

**Root-cause classification:** `Async misuse` — but the *mechanism* that produces this exact error is a
call site that invokes `.table()` on the **un-awaited coroutine** `get_supabase_async()` rather than on
the awaited client. The repository's live code is written correctly (`await get_supabase_async()`), so the
error surfaced at runtime indicates the executing code path differs from the on-disk source at the
offending line, OR a code path that calls `get_supabase_async()` and immediately chains `.table()` **without**
the preceding `await`.

**VERIFIED fact:** `core/database.py` is correct and every on-disk call site awaits `get_supabase_async()`.
The `'coroutine' object has no attribute 'table'` message is the literal signature of
`(get_supabase_async()).table(...)` — i.e. the `await` keyword is MISSING at the runtime call site. Because
the on-disk source at `agent_service.py:1356` / `interactive_wait.py:315` DOES contain `await`, the running
binary is **stale relative to the repository** at those lines (deployment/restart did not pick up the
awaited form, or a pre-await refactor left a divergent copy).
→ **Classification: Async misuse (await missing at runtime call site).** The *missing-await* form is not
present in the committed source, so the discrepancy is between the running process and the repo.
**NOT VERIFIED:** whether the running process is an old deploy vs. a local edit not committed — git is
excluded from this investigation, so the exact divergence cannot be pinned to a commit.

### Issue 2 — `RuntimeWarning: coroutine 'get_supabase_async' was never awaited`

**Where the coroutine is created:** `get_supabase_async()` is called at `agent_service.py:1356` (resume
block) and `interactive_wait.py:315`. Both **are** awaited in the on-disk source, so the warning is
emitted by the **same un-awaited invocation** that produces Issue 1 — a coroutine object is created and
then, because the `await` is absent at runtime, it is garbage-collected without being awaited.

**Why it is never awaited:** identical to Issue 1 — the `await` is absent at the runtime call site, so the
coroutine returned by `get_supabase_async()` is never driven to completion.

**Relationship to Issue 1:** It is the **same defect** seen from two angles. Issue 1 is the *consequence*
(when the code then tries `.table()` on the un-awaited coroutine); Issue 2 is the *warning* emitted when
that coroutine is GC'd. They are NOT independent — same missing `await`.

**Classification:** `Async misuse` (same root as Issue 1).

### Issue 3 — `cannot import name 'load_workspace_context' from services.server_service`

**Original owner:** `load_workspace_context` is the **canonical module-level async function** at
`services/capability_service.py:181` (its docstring: "authoritative platform context (port/subdomain/SSL/
gateway)"). It is the function `executor.py:18` imports: `from services.capability_service import
detect_capabilities, load_workspace_context`.

**Current owner / duplication:** `server_service.py` ALSO defines a `load_workspace_context` — but as a
**`@staticmethod` on the `ServerService` class** (`server_service.py:293`, `async def load_workspace_context`
indented under `class ServerService`). It is therefore **not importable at module level** from
`services.server_service`.

**The broken import:** `agent_service.py:1801` does
`from services.server_service import WorkspaceContext, load_workspace_context`.
- `WorkspaceContext` IS module-level in `server_service.py:30` (✓ import succeeds).
- `load_workspace_context` is a class `@staticmethod` (✗ NOT module-level) → **ImportError**.

**All callers/imports:**
- `executor.py:18` → `from services.capability_service import ... load_workspace_context` (✓ correct owner)
- `executor.py:440` → `await load_workspace_context(...)` (uses capability_service's)
- `agent_service.py:1801` → `from services.server_service import ... load_workspace_context` (✗ broken —
  server_service only has it as a staticmethod)
- `agent_service.py:1804` → `await load_workspace_context(...)` (never reached; import fails first)
- Tests reference `services.capability_service.load_workspace_context` (test_deployment_contract.py:84/132/186/239,
  test_port_discipline.py:77/130/173, conftest.py:41) — all using the **correct** owner.

**Classification:** `Broken refactor` / `Partial migration`. `load_workspace_context` exists in TWO places:
the canonical `capability_service.py` module function and a duplicate `ServerService.load_workspace_context`
staticmethod. `agent_service.py` was edited to import it from `server_service` (where it is not
module-level), instead of from `capability_service` (where `executor.py` correctly gets it). This is a
partial/inconsistent migration: the function was (or is being) duplicated onto `ServerService`, but the
`agent_service.py` import was pointed at the wrong module and at a non-exported symbol.

### Issue 4 — `workspace_platform is absent` (platform context missing for deployment intent)

**Execution trace:**
```
run_agent_pipeline (agent_service.py:1342)
  └─ server intent + workspace_id
       └─ pre-plan context load (agent_service.py:1799-1810)
            └─ from services.server_service import WorkspaceContext, load_workspace_context  (1801)  ← ImportError (Issue 3)
            └─ [run_agent_pipeline] pre-plan context load failed: <ImportError>               (1811)
       → preplanned_workspace_context = None
  └─ build_plan(...) → generate_plan(...)  (agent_llm.py:1772)
       └─ constitution.check_platform_context(context.get("workspace_platform"))            (agent_llm.py:1787)
            └─ workspace_platform is None → PlatformContextMissingError
            └─ "Cannot proceed without authoritative port, subdomain, protocol, base_url."
```

**Where workspace_platform SHOULD be created/populated:**
- Authoritative source: `load_workspace_context` (capability_service.py:181) builds a `WorkspaceContext`
  carrying `port/subdomain/protocol/gateway_available/ssl_enabled/runtime_type`, and `.as_dict()` is what
  should be assigned to `coordinator_context["workspace_platform"]` (executor.py:435 does exactly this on
  the executor refresh path).
- In `run_agent_pipeline`, `preplanned_workspace_context` is meant to be produced by the
  `agent_service.py:1804` `load_workspace_context(...)` call and then fed into `build_plan(workspace_context=...)`
  → `coordinator_context["workspace_platform"] = workspace_context.as_dict()` (planner.py, server branch).
- `RedisService.get_sync_client()` (`ws:{workspace_id}:port`, `ws:active`) is the actual data source for
  port/gateway (capability_service.py:160-170, server_service.py:298-306).
- `WorkspaceService.get_workspace_by_id` (agent_service.py:1803) supplies domain/name/slug for subdomain.

**Where it becomes None:** the `agent_service.py:1801` import raises `ImportError` (Issue 3), so
`preplanned_workspace_context` stays `None` and `workspace_platform` is never populated. The planner then
hits the constitution guard with `workspace_platform is None`.

**Classification:** This is NOT an independent failure. It is a **direct consequence of Issue 3** — the
pre-plan context load fails because the import fails, so the platform context is absent. (It is also
exacerbated by the independent Issue 1/2 if the resume/interaction-state DB reads also raise, but the
specific "workspace_platform is absent" message traces to the Issue-3 import failure in the pre-plan block.)

### Issue 5 — `name 'get_supabase_async' is not defined`

**Execution trace:** `[wait] failed to load interaction state: name 'get_supabase_async' is not defined`
→ `interactive_wait.py:329` (inside `JobInteractionState._load_state`, line 308-327).

**Exact location:** `interactive_wait.py:308-327` calls `get_supabase_async()` (the `.table()` chain) but
**never imported it in that function's scope**. The function does `from core.database import get_supabase`
(line 311) but NOT `get_supabase_async`. (Contrast: `interactive_wait.py:343/359` `_persist_state` DO import
`get_supabase_async` at line 316/339 and work.)

**Classification:** `Missing dependency` (scope error). `JobInteractionState._load_state` uses
`get_supabase_async` without importing it; only its sibling `get_supabase` (sync) is imported there. This is
an independent import-scope bug, separate from Issue 1 (which is about await) and Issue 3 (which is about a
wrong-module import). Note: because `get_supabase_async` is undefined here, the code never even reaches the
`await`/`.table()` chain, so Issue 5 is distinct from Issue 1 despite both touching the interaction-state
load — Issue 1 is "coroutine has no .table" (await missing), Issue 5 is "name not defined" (import missing).

---

## B. COMPLETE EXECUTION GRAPH

```
run_agent_pipeline (agent_service.py:1342)
├─ [RESUME] get_supabase_async().table("jobs")...   (1356)  → Issue 1/2 (await missing at runtime)
├─ [PRE-PLAN CONTEXT] (1799)
│    from services.server_service import WorkspaceContext, load_workspace_context  (1801)
│        → ImportError: load_workspace_context not module-level in server_service  → Issue 3
│    preplanned_workspace_context = None
├─ build_plan(...) (1813)
│    └─ generate_plan (agent_llm.py:1772)
│         └─ constitution.check_platform_context(workspace_platform=None)  (1787)
│              → PlatformContextMissingError "workspace_platform is absent"  → Issue 4
└─ [EXECUTION] _execute_with_lock → executor.run_server_execution
     └─ executor context refresh (executor.py:429) get_supabase_async().table("workspaces")  ✓ (awaited)
     └─ load_workspace_context (capability_service.py:181)  ✓ (correct import)

[WAIT] JobInteractionState._load_state (interactive_wait.py:308)
     get_supabase_async()  ← NOT imported in this function scope  → Issue 5
     (sibling _persist_state at 343 imports it and works)
```

## C. DEPENDENCY GRAPH

```
Resume ───────────────► Supabase (get_supabase_async, core/database.py:81)
Interaction State ────► Supabase (get_supabase_async)  +  JobInteractionState (interactive_wait.py)
Workspace Context ───► load_workspace_context (capability_service.py:181)
                     ├─ RedisService (ws:{id}:port, ws:active)
                     ├─ WorkspaceService.get_workspace_by_id (domain/name/slug)
                     └─ SSHService (SSL cert check)
Platform Context ────► WorkspaceContext (capability_service.py:44 / server_service.py:30)
                     └─ consumed by constitution.check_platform_context (guard)
Supabase ────────────► core/database.py (get_supabase / get_supabase_async, _AsyncClient/_AsyncBuilder)
```

Subsystem dependencies:
- `agent_service` depends on `core.database` (Supabase) AND on `capability_service.load_workspace_context`
  (but wrongly imports it from `server_service`).
- `executor` correctly depends on `capability_service.load_workspace_context`.
- `interactive_wait` depends on `core.database.get_supabase_async` (partially — `_load_state` missing the import).
- `server_service` defines a duplicate `load_workspace_context` staticmethod + its own `WorkspaceContext`.

## D. REPOSITORY EVIDENCE (file:line)

- `core/database.py:81` — `async def get_supabase_async() -> _AsyncClient` (correct).
- `core/database.py:72-78` — `_AsyncClient.table` returns `_AsyncBuilder`; `.execute` is coroutine.
- `agent_service.py:1356` — `await get_supabase_async().table("jobs")...` (awaited in source; runtime diverges).
- `agent_service.py:1801` — `from services.server_service import WorkspaceContext, load_workspace_context`
  (broken: `load_workspace_context` is a `ServerService` `@staticmethod`, server_service.py:293, not module-level).
- `server_service.py:30` — module-level `WorkspaceContext` (importable); `server_service.py:293` —
  `async def load_workspace_context` as `@staticmethod` (NOT importable at module level).
- `capability_service.py:181` — canonical module-level `async def load_workspace_context` (correct owner).
- `executor.py:18` — `from services.capability_service import detect_capabilities, load_workspace_context` (correct).
- `interactive_wait.py:311` — `from core.database import get_supabase` (missing `get_supabase_async`).
- `interactive_wait.py:315` — uses `get_supabase_async()` (undefined in this scope) → Issue 5.
- `agent_llm.py:1787` — `constitution.check_platform_context(context.get("workspace_platform"))` raises when None.
- `planner.py` server branch — `coordinator_context["workspace_platform"] = workspace_context.as_dict()`.

## E. VERIFIED ROOT CAUSE (per issue)

1. **Issue 1** — `Async misuse`: `.table()` is called on the un-awaited `get_supabase_async()` coroutine.
   On-disk source awaits it; the running process does not at the offending call site → stale/divergent
   runtime vs repo. (NOT VERIFIED: which deploy/commit diverged — git excluded.)
2. **Issue 2** — Same missing-`await` as Issue 1; the coroutine is GC'd un-awaited → RuntimeWarning. Not
   independent.
3. **Issue 3** — `Broken refactor` / `Partial migration`: `agent_service.py:1801` imports
   `load_workspace_context` from `services.server_service`, but it exists there only as a `ServerService`
   `@staticmethod` (server_service.py:293), not a module-level export. The canonical module-level function
   is `capability_service.py:181`.
4. **Issue 4** — Consequence of Issue 3: the pre-plan `load_workspace_context` import fails, so
   `workspace_platform` is never populated, and the planner's constitution guard rejects the deployment
   intent. (Independent data-layer causes — Redis/DB/workspace missing — are NOT evidenced by the logs;
   the log's "pre-plan context load failed" exception is the ImportError from Issue 3.)
5. **Issue 5** — `Missing dependency` (scope error): `JobInteractionState._load_state`
   (interactive_wait.py:308) calls `get_supabase_async()` without importing it in that function (only the
   sync `get_supabase` is imported at line 311).

## F. RELATIONSHIP BETWEEN THE FOUR ERRORS

- **Issue 1 ⇄ Issue 2**: same root (missing `await` on `get_supabase_async()`). One defect, two symptoms.
- **Issue 3 → Issue 4**: Issue 4 is a *direct downstream consequence* of Issue 3 (import failure → no
  platform context → planner guard trips).
- **Issue 5**: independent of 1/2/3/4. It is a separate missing-import in a different function
  (`_load_state`), and it manifests as "name not defined" (never reaches the await/`.table` chain), so it
  is distinct from Issue 1.
- **Net:** 3 underlying defects across 5 symptoms:
  (a) missing `await` on `get_supabase_async()` at the resume/interaction-state runtime call sites [Issues 1+2];
  (b) wrong-module / non-exported import of `load_workspace_context` in `agent_service.py` [Issue 3 → Issue 4];
  (c) missing `get_supabase_async` import in `interactive_wait._load_state` [Issue 5].

## G. RECOMMENDED FIX ORDER ONLY (no code / no patches)

1. **Fix Issue 3 first** (it cascades into Issue 4 and blocks server planning entirely). Point
   `agent_service.py:1801` at the canonical `capability_service.load_workspace_context` (matching
   `executor.py:18`), and reconcile the duplicate `server_service.ServerService.load_workspace_context`
   staticmethod with the canonical function (keep one source of truth).
2. **Fix Issue 5** (missing import) — add `get_supabase_async` to the import inside
   `interactive_wait.JobInteractionState._load_state` (line ~311), alongside the existing `get_supabase`.
3. **Fix Issues 1 & 2** (missing `await`) — ensure every `get_supabase_async()` call site that chains
   `.table()...` is `await`ed; redeploy/restart so the running process matches the awaited source. Verify
   the resume block (agent_service.py:1356) and interaction-state load (interactive_wait.py:315) are
   awaited at runtime, not just on disk.
4. **Regression guard**: add a startup import smoke-test that imports `agent_service`, `executor`,
   `interactive_wait`, `server_service` and asserts `load_workspace_context` is importable from wherever
   `agent_service` expects it; and a lint/CI check that `get_supabase_async()` is never called without a
   preceding `await` in async functions.

**NOT VERIFIED:** (i) whether the running process is an old deploy vs. an uncommitted local edit at the
await-missing lines (git excluded); (ii) whether Redis/DB actually holds the port for a given workspace
(no runtime Redis/DB data available — only the import/await defects are evidenced by the logs).
