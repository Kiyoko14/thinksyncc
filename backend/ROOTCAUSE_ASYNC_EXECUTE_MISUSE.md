# STRICT ROOT-CAUSE INVESTIGATION — ASYNC EXECUTE() COROUTINE MISUSE

**Rules honored:** no git, no file modification, no patches, no refactor. Conclusions backed by code
location + execution trace + AST. The previous `.table()` bug is confirmed resolved (those call sites now
await the client); execution progressed one link deeper and now fails at the terminal `.execute()`.

---

## A. EXECUTION TRACE — `[resume]` path producing `'coroutine' object has no attribute 'data'`

```
run_agent_pipeline(job_id, ...)                              services/agent_service.py:1342
  └─ resume block (Sprint 3A)                                agent_service.py:1349-1353
       try:
         from core.database import get_supabase             agent_service.py:1354
         result = (                                          agent_service.py:1355
             (await get_supabase_async())                    agent_service.py:1356   <- client now awaited (prior fix)
             .table("jobs")                                   agent_service.py:1357
             .select("status","execution_cursor",...)         agent_service.py:1358
             .eq("id", job_id)                                agent_service.py:1359
             .limit(1)                                        agent_service.py:1360
             .execute()                                       agent_service.py:1361   <<< .execute() is NOT awaited
         )                                                    agent_service.py:1362
         # .execute() (core/database.py:56, `async def execute`) returns a COROUTINE, not a result object
         if result.data:                                      agent_service.py:1366   <<< EXACT FAILING LINE
            row = result.data[0]                              agent_service.py:1367
       → `result` is a coroutine → 'coroutine' object has no attribute 'data'
       → RuntimeWarning: coroutine '_AsyncBuilder.execute' was never awaited
       except Exception as exc:                              agent_service.py:~1368
         logger.warning("[resume] failed to check resume state: %s", exc)
```

Every function in order: `run_agent_pipeline` → `ResumeManager`/inline resume block → `core.database.get_supabase_async` → `_AsyncClient.__getattr__("table")` → `_AsyncBuilder` chain → `_AsyncBuilder.execute` (coroutine, un-awaited) → `result.data` access.

## B. REPOSITORY EVIDENCE

- `core/database.py:56` — `async def execute(self, *args, **kwargs) -> Any: return await asyncio.to_thread(self._builder.execute, ...)`. **`.execute()` on `_AsyncBuilder` is a coroutine and MUST be awaited.**
- `core/database.py:29-57` — `_AsyncBuilder` wraps the sync Supabase builder; `execute()` is the only
  terminal that runs the query (in a thread). All intermediate methods return `_AsyncBuilder`.
- `agent_service.py:1361` — `.execute()` called WITHOUT `await`; `agent_service.py:1366` accesses `result.data`.
- Same pattern repeated at 50 other sites (see §E).

## C. GREP RESULTS (Task 3A)

- `grep -rn "execute()"` across repo: **222 lines** contain `execute()`. Most are the SYNC client
  `get_supabase().table(...).execute()` (correct — sync `execute` returns a result, not a coroutine).
- `grep -rn "get_supabase_async"` + manual/context inspection confirms the async-chain `.execute()` sites
  are the 51 listed in §E. Single grep alone is insufficient (sync vs async clients share `.execute()`),
  which is why AST was required.

## D. AST RESULTS (Task 3B)

AST detector (authoritative): find every `Call` to `.execute()` whose chain root is literally
`get_supabase_async()` (via `get_supabase_async()` or `(await get_supabase_async())`), and check whether
that `.execute()` `Call` is the direct value of an `Await` node.

- **Broken (`.execute()` NOT awaited): 51**
- **Correct (`.execute()` awaited): 1** — `services/conversation_reliability.py:604`
  (`await (await get_supabase_async()).table(table).select("*").limit(0).execute()`).
- The 16 sites in `server_service.py` / `deployment_service.py` / `workspace_service.py` that use a
  `supabase` alias were initially flagged but VERIFIED FALSE POSITIVES: their enclosing function assigns
  `supabase = get_supabase()` (the **sync** client), where `.execute()` is sync and correct. The alias-based
  false positive was eliminated by checking the enclosing-function assignment (see investigation notes).

## E. EVERY BROKEN execute() OCCURRENCE (51)

| File | Lines (`.execute()` not awaited) |
|------|----------------------------------|
| `models/agent.py` | 628, 639, 658, 714 |
| `models/approval.py` | 298, 306, 322, 344, 361 |
| `models/conversation.py` | 191, 210 |
| `services/agent_service.py` | 1356 |
| `services/approval_engine.py` | 304, 328, 361 |
| `services/context_engine.py` | 205, 222, 478 |
| `services/conversation_audit.py` | 83 |
| `services/conversation_continuation.py` | 132 |
| `services/conversation_reliability.py` | 148, 191, 380, 417, 530 |
| `services/event_wait_engine.py` | 387, 419, 438, 582, 695 |
| `services/interactive_wait.py` | 316, 343, 359 |
| `services/repository_index.py` | 178, 201 |
| `services/requirement_discovery.py` | 121, 132, 151, 178, 256, 270, 290, 317, 330, 349, 1030, 1050 |
| `services/resume_manager.py` | 71, 204, 229 |
| `services/timeout_manager.py` | 177 |

Each is the form `(await get_supabase_async()).table(...).[select|update|insert|upsert|delete](...).execute()`
**without `await` on `.execute()`**. The terminal `.execute()` returns a coroutine; any `.data`/`.error`
access on it fails.

## F. ROOT CAUSE CLASSIFICATION

**A. Missing await on `.execute()`** (async builder terminal). This is a DISTINCT but RELATED defect to the
prior `.table()` precedence bug. The prior fix correctly awaited `get_supabase_async()`, but the chain's
terminal `.execute()` was left un-awaited. `_AsyncBuilder.execute` is an `async def`, so it returns a
coroutine; without `await`, `result` is a coroutine and `result.data` raises
`'coroutine' object has no attribute 'data'`.

Rejected hypotheses (verified NOT the cause):
- **B. Builder returns non-awaitable** — false; `_AsyncBuilder.execute` is correctly `async def`.
- **C. Wrong async wrapper** — false; `_AsyncBuilder`/`_AsyncClient` are correct and consistent.
- **D. Shadowed execute()** — false; single `execute` on `_AsyncBuilder`, no shadowing.
- **E. Duplicate builder implementation** — false; one `_AsyncBuilder`.
- **F. Import alias** — N/A (the literal `get_supabase_async()` root is used; alias false positives ruled out).
- **G. Other** — not applicable.

## G. MINIMAL CORRECTION STRATEGY (NO CODE)

For each of the 51 sites, await the terminal `.execute()`:
- **If the chain is inline/one expression:** wrap the whole chain in `await (...)`:
  `result = await ( (await get_supabase_async()).table(...).execute() )` — or, cleaner and already
  proven-correct in the repo (`conversation_reliability.py:604`), use the nested form
  `result = await (await get_supabase_async()).table(...).execute()`. Either adds exactly one `await` on the
  terminal `.execute()`.
- **If the chain is multi-line parenthesized** (e.g. `agent_service.py:1355-1362`): place `await` at the
  opening of the parenthesized expression, i.e. `result = await ( (await get_supabase_async()) .table(...) ...
  .execute() )`. Only the evaluation order of the terminal `.execute()` changes; query logic, filters,
  selects, ordering, eq/limit, exception handling, and return values are untouched.
- Do NOT touch `core/database._AsyncBuilder.execute` (correct), the sync `get_supabase()` paths, SQL,
  migrations, tests, DE/Planner/Structured Output/Routing/Orchestration.

## H. CONFIDENCE

**HIGH (production-grade).** Reproduced in principle by the AST semantics; the exact failing line
(`agent_service.py:1361` `.execute()` → `:1366` `result.data`) is identified; `_AsyncBuilder.execute` is
proven `async def` (core/database.py:56); AST enumerates all 51 broken sites repo-wide and excludes
false-positive sync-client sites; the 1 correct site is identified. The defect class is purely "missing
`await` on the terminal builder call" and is provably fixable by adding `await` at the chain terminus.

## I. NOT VERIFIED

- Whether any of the 51 sites is masked at runtime by an earlier failure in the same function — NOT VERIFIED
  per-site; irrelevant to root cause (each is independently a missing-await defect and will raise on reach).
- Whether the live runtime process was rebuilt after the `.table()` fixes — NOT VERIFIED (no build/git
  access); but the AST proof shows 51 broken `.execute()` in source regardless, so the runtime error is fully
  explained by source.
- Dynamic attribute access to `execute` (e.g. `getattr(builder,"execute")()`) — not found in static scan;
  NOT VERIFIED by static AST (dynamic dispatch generally unverifiable), but no evidence of such usage exists.

---

## TASK 7 — WHY RUNTIME CHANGED (technical explanation)

- **Before `.table()` fix:** the chain was `await get_supabase_async().table(...)`. Python parses this as
  `await (get_supabase_async().table(...))`. `get_supabase_async()` returns a **coroutine**; `.table()` is
  called on it → `'coroutine' object has no attribute 'table'`. The failure happened at the **second link**
  of the chain, before `.execute()` was ever reached.
- **After `.table()` fix:** the chain is `(await get_supabase_async()).table(...).execute()`. Now
  `get_supabase_async()` is awaited → returns `_AsyncClient`; `.table()` returns `_AsyncBuilder`; the chain
  proceeds correctly through `.select()/.eq()/.limit()` (all return `_AsyncBuilder`) until the terminal
  `.execute()`. But `.execute()` is an `async def` and was **never awaited** → it returns a **coroutine**.
  The failure now occurs at the **last link**: `result.data` → `'coroutine' object has no attribute 'data'`,
  with `RuntimeWarning: coroutine '_AsyncBuilder.execute' was never awaited`.

So fixing the `.table()` precedence bug exposed the **next latent defect one link deeper** in the same chain:
the terminal `.execute()` was un-awaited all along. Both are the same root family (async evaluation-order
misuse); the prior fix advanced execution to the next broken link. This is expected "peeling the onion"
behavior, not a regression.

## TASK 8 — EXECUTION DIAGRAM (coroutine escape point marked)

```
get_supabase_async()           [async def -> coroutine]
   │  (await ...)              [PRIOR FIX: now awaited -> _AsyncClient]   ✅ fixed
   ▼
.table(...)                    [_AsyncClient.__getattr__ -> _AsyncBuilder] ✅
.select(...) / .eq(...) / .limit(...)   [_AsyncBuilder -> _AsyncBuilder]   ✅
   │
   ▼
.execute()                     [_AsyncBuilder.execute is `async def` -> RETURNS COROUTINE]
   │  ★ MISSING await HERE ★   <-- COROUTINE ESCAPES (result is a coroutine)
   ▼
result  (= coroutine)
   │
   ▼
result.data                    ✗ 'coroutine' object has no attribute 'data'
```

The escape point is the terminal `.execute()` call (no `await`). After this, `result` is a coroutine and any
`.data` access fails.
