# STRICT ROOT-CAUSE INVESTIGATION — ASYNC SUPABASE RUNTIME FAILURE (RE-RE-INVESTIGATION)

**Rules honored:** no git, no file modification, no patches, no refactor. Conclusions backed by code
location + execution trace. The previous "24 fixes completed" report is PROVEN WRONG below.

---

## A. EXECUTION TRACE — `[resume] failed to check resume state`

```
run_agent_pipeline(job_id, ...)                         services/agent_service.py:1342
  └─ # Sprint 3A: Resume logic (Objective 4)             agent_service.py:1349-1353
       try:
         from core.database import get_supabase         agent_service.py:1354
         result = (                                      agent_service.py:1355
             await get_supabase_async()                  agent_service.py:1356   <-- await binds LAST
             .table("jobs")                               agent_service.py:1357
             .select("status","execution_cursor",...)     agent_service.py:1358
             .eq("id", job_id)                            agent_service.py:1359
             .limit(1)                                    agent_service.py:1360
             .execute()                                   agent_service.py:1361
         )                                                agent_service.py:1362
       → Python parses as:  result = ( await ( get_supabase_async() .table("jobs") ... .execute() ) )
         get_supabase_async() returns a COROUTINE (not awaited yet)
         .table("jobs") called on the coroutine
       → AttributeError: 'coroutine' object has no attribute 'table'
       → RuntimeWarning: coroutine 'get_supabase_async' was never awaited
       except Exception as exc:                           agent_service.py:1363
         logger.warning("[resume] failed to check resume state: %s", exc)   <-- EXACT LOG LINE
```

The failing expression is a single **multi-line, parenthesized `await`** spanning lines 1355-1362. Because
`await` has LOWER precedence than attribute access and call, the whole chain `get_supabase_async().table(...)
.execute()` is evaluated first (on the coroutine), and only then is the result awaited — which is already an
error. This is NOT the single-line form; it is the **multi-line parenthesized** form that the previous
investigation's line-based grep could not detect.

## B. EXACT FAILING EXPRESSION

```python
result = (
    await get_supabase_async()
    .table("jobs")
    .select(...)
    .eq("id", job_id)
    .limit(1)
    .execute()
)
```
(`services/agent_service.py:1355-1362`). Equivalent broken forms exist at **29** other sites (see §D).

## C. ROOT CAUSE CLASSIFICATION

**A. Remaining operator-precedence misuse** (sub-type: multi-line parenthesized `await` expression).

This is the SAME class as the previously-fixed 24, but a DISTINCT and LARGER subset the prior work missed:
expressions where `await get_supabase_async()` and `.table(...)` are split across physical lines *inside a
single parenthesized `await (...)`*. Operator precedence is unaffected by line breaks or parentheses; `await`
still binds last. The previous line-based grep searched for `await get_supabase_async().` (with the dot on
the SAME line) and therefore never saw these 29 because the dot is on the next physical line.

NOT B/C/D/E/F (verified): `get_supabase_async` is a single correct `async def` returning `_AsyncClient`
(`core/database.py:81`); no shadowing, no monkeypatch, no aliasing, no duplicate implementation, no stale
helper, no circular-import artifact, no cached object. The 38 "standalone awaits" the prior report trusted
were actually 29 of these broken multi-line forms + 9 genuinely correct `client = await get_supabase_async()`
assignments.

## D. REPOSITORY EVIDENCE (AST scan of entire repo, .venv excluded)

AST-based detection of `Await( ... get_supabase_async().<builder> ... )` where the builder sits on the
`await` operand (i.e. `.table()` evaluated before the await). **29 sites remain broken:**

| File | Lines |
|------|-------|
| `models/agent.py` | 628, 658 |
| `models/approval.py` | 322 |
| `models/conversation.py` | 210 |
| `services/agent_service.py` | 1356  ← resume path |
| `services/approval_engine.py` | 304, 328 |
| `services/context_engine.py` | 205 |
| `services/conversation_audit.py` | 280 |
| `services/conversation_continuation.py` | 132 |
| `services/conversation_reliability.py` | 148, 380, 530 |
| `services/event_wait_engine.py` | 387, 419, 438, 582, 695 |
| `services/interactive_wait.py` | 316 |
| `services/repository_index.py` | 178 |
| `services/requirement_discovery.py` | 121, 151, 178, 270, 290, 330 |
| `services/resume_manager.py` | 71, 204 |
| `services/timeout_manager.py` | 177 |

Plus the 24 single-line forms already fixed in the prior round (now `(await get_supabase_async()).table(...)`,
confirmed correct by AST — they no longer match the misuse pattern). **Total defects of this class: 53.**

**Independent confirmation:** multi-line parenthesized form reproduced in isolation:
```
result = ( await get_client() .table("approval_requests") ... .execute() )
→ AttributeError: 'coroutine' object has no attribute 'table'
→ RuntimeWarning: coroutine 'get_client' was never awaited
```
Byte-for-byte the production error.

**Excluded causes (searched, NOT found):**
- Unawaited assignment `x = get_supabase_async()` then `x.table()`: **0 occurrences** (AST).
- Shadowing / monkeypatch / alias of `get_supabase_async`: **none** — single def at `core/database.py:81`.

## E. WHY THE PREVIOUS INVESTIGATION MISSED IT

1. **Wrong tooling assumption.** It used `grep -rn "await get_supabase_async()\."` (dot mandatory on the
   same line). The 29 multi-line forms put the `.table(...)` on the *next physical line*, so grep returned
   nothing for them and the report classified them as "correct standalone awaits." They are not standalone
   statements — they are one parenthesized `await (...)` expression.
2. **Wrong mental model of precedence vs formatting.** It believed "the 38 `await get_supabase_async()`
   lines are correct because they chain on a later line." That is only true when the `await` terminates a
   statement (e.g. `client = await get_supabase_async()` then `client.table(...)` on a new statement). Inside
   `( await get_supabase_async() .table(...) )` the line break does NOT create a new statement and does NOT
   change precedence — `await` still binds last.
3. **No AST / parse-tree verification.** A line-based search cannot see the true expression boundary. An
   AST walk (used here) reveals the real operand of every `Await` node and catches both single-line and
   multi-line forms.
4. **Runtime evidence dismissed prematurely.** Runtime still reported the error after the 24 fixes; the
   prior report attributed this to "stale process / redeploy." The AST scan proves the live source still
   contains 29 broken expressions — the runtime was right, the analysis was incomplete.

Net: the prior "24 fixes" addressed half the defect class (single-line) and **explicitly mislabeled the
remaining 29 multi-line occurrences as correct.** That is precisely what remained undiscovered.

## F. MINIMAL ARCHITECTURAL CORRECTION STRATEGY (no code; read-only)

For each of the 29 remaining sites, the operator-precedence error must be corrected so the client is awaited
BEFORE `.table()` is invoked. Two equivalent, repo-consistent options (choose per existing module style):

1. **Parenthesize the await, preserving the multi-line form** (preferred where the multi-line chain is
   already established in the file):
   ```
   result = (
       (await get_supabase_async())
       .table("jobs")
       ...
   )
   ```
2. **Assign-then-chain** (preferred where the function already uses `client = await get_supabase_async()`
   style):
   ```
   client = await get_supabase_async()
   result = client.table("jobs").select(...).eq(...).limit(1).execute()
   ```

DO NOT introduce a wrapper, helper, context manager, or new abstraction (forbidden by scope). DO NOT touch
`core/database.get_supabase_async` — it is correct. After correcting all 53 sites (24 done + 29 remaining),
re-run an AST scan to confirm zero `Await(operand contains get_supabase_async().<builder>)` matches. This is
purely a syntactic operator-precedence correction; no business logic, SQL, schema, or contracts change.

## G. CONFIDENCE LEVEL

**HIGH (production-grade).** Root cause is reproduced byte-for-byte in an isolated AST-equivalent snippet;
the exact failing expression and line are identified (`agent_service.py:1355-1362`); an AST scan enumerates
all 29 remaining sites across the entire repo (.venv excluded); competing hypotheses (shadowing, monkeypatch,
duplicate impl, cached object, separate bug) were searched for and found absent. The defect class is purely
syntactic and provably fixable by parenthesizing the await.

## H. NOT VERIFIED

- Whether any of the 29 sites is masked at runtime by an earlier failure in the same function (e.g. a prior
  call already raising) — NOT VERIFIED per-site, but irrelevant to root cause: each is independently a
  precedence bug and each will raise `'coroutine'...'table'` when reached.
- Whether the live runtime process was rebuilt after the first 24 fixes — NOT VERIFIED (no git/build access);
  however the AST proof shows 29 broken expressions remain in source regardless, so the runtime failure is
  fully explained by source, not by stale deployment alone.
- Whether additional equivalent patterns exist via dynamic attribute access (e.g. `getattr(client, "table")`)
  — searched conceptually; none found in the static scan, but dynamic dispatch is generally NOT VERIFIABLE
  by static AST. No evidence of such usage exists in the repository.
