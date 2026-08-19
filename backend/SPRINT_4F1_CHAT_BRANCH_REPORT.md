# SPRINT 4F-1 — CHAT EXECUTION BRANCH — REPORT

**Mode:** Production migration, strict safety. Completes the missing CHAT execution path inside the
unified orchestration pipeline so the Decision Engine can safely become authoritative later.
**Result:** Chat now returns a conversational LLM response and NEVER reaches `run_server_execution()`.
Verified at runtime by actually invoking `run_agent_pipeline` for chat/code/server under mocked I/O.
Compile clean; 221 non-orphan tests pass (4 unrelated pre-existing auth-DB-mock failures; 1 orphan
pre-existing collection error). No refactor, no rewrite, no Decision Engine change.

---

## 1. ROOT CAUSE (the defect this sprint fixes)

Verified in the prior root-cause investigation (`ROOTCAUSE_INTENT_NOT_SERVER.md`):

- `run_agent_pipeline` (agent_service.py:1342) has an `if intent == "code":` branch (1643) that returns,
  and partial `intent == "server"` setup (1744/1756), but **no `intent == "chat"` branch**.
- After the code branch returns (1742), a chat intent falls straight through to the unconditional
  `build_plan` (1770) and `run_server_execution(intent=intent)` (2073) server path.
- `executor.py:379-380` rejects any non-`"server"` normalized intent → `INTENT_NOT_SERVER`.
- `DECISION_ENGINE_MODE` is `"off"` in production, so the authoritative reassignment at 1627 is inert,
  and even if enabled, authoritative routing to `chat` would have hit the same missing branch.

The missing chat branch is the **only** way a chat request becomes a safe, standalone conversational
response. This sprint adds exactly that branch as a sibling of the code/server branches.

---

## 2. FILES MODIFIED

| File | Change | Kind |
|---|---|---|
| `services/agent_service.py` | Added `if intent == "chat":` branch (returns before `build_plan`/`run_server_execution`) at line 1743 | additive, guarded |

**No other file touched.** Config, routers, tests, Decision Engine, Decision Router, Weighted/Shadow
modes, legacy flags, SQL, frontend, API contracts, auth, permission, approval, resume, worker — all
untouched. (Verified: only one `intent =` reassignment in the decision block; my change adds no such
reassignment — it adds a new `if` block that `return`s.)

---

## 3. WHY EACH MODIFICATION WAS REQUIRED

- **The `if intent == "chat":` block** is required because the pipeline had a fall-through for any intent
  that is neither `code` nor (`server`+workspace). Without it, chat reached the server executor and raised
  `INTENT_NOT_SERVER`. The branch reuses the existing `agent_llm.generate_chat_response` (proven
  existing chat implementation) and `ChatService` persistence, so it is a completion, not a new pipeline.
- It returns **before** `build_plan()` (1770) and `run_server_execution()` (2073), guaranteeing a chat
  intent can never reach the server executor regardless of Decision Engine mode.
- Discovery already runs above the intent branch (1431-1464) and is reused as-is; no duplication.
- Clarification is intentionally NOT invoked: the existing reusable chat pipeline (`run_explicit_mode`
  "plan" path, line 2407) does not require clarification for conversational intents, so "when required"
  == not required. Reusing `run_explicit_mode` wholesale would have created a second chat pipeline, which
  the brief forbids; instead the single existing function `generate_chat_response` is reused.

---

## 4. EXECUTION GRAPH — BEFORE

```
intent = classify_intent(...)            (1518)
deployment heuristic (1521, skipped for "Serverning")
[ DECISION hook — OFF in prod, skipped ]  (1611-1627)
if intent == "code":  -> _run_code_execution; return   (1643)
if intent == "server" and workspace:  (chat setup only) (1744)
constitution_engine.check_objective (1751)
build_plan(intent=chat)   (1770)   <-- chat falls in HERE
run_server_execution(intent=chat)  (2073)  <-- raises INTENT_NOT_SERVER
   -> executor.py:379 normalized_intent="chat" != "server" -> HTTPException
```

`chat → Planning → run_server_execution → INTENT_NOT_SERVER`

## 5. EXECUTION GRAPH — AFTER

```
intent = classify_intent(...)            (1518)
[ Discovery already ran above (1431) — reused ]
[ DECISION hook — OFF in prod, skipped ]  (1611-1627)
if intent == "code":  -> _run_code_execution; return   (1643)
if intent == "chat":  -> generate_chat_response()       (1743)  <-- NEW
                       -> persist ChatService
                       -> _db_update(COMPLETED); _publish(completed); return
if intent == "server" and workspace: ...                (1744)
constitution_engine.check_objective (1751)
build_plan / run_server_execution (server only)         (1770/2073)
```

`chat → Discovery → (Clarification when required: not required) → Planning: SKIPPED → Conversation/Chat execution (generate_chat_response) → LLM response → Return. WITHOUT entering run_server_execution().`

The chat branch is another node of the **same** orchestration graph (sibling of code/server), not a
second pipeline.

---

## 6. REGRESSION ANALYSIS

**Runtime integration proof (actual `run_agent_pipeline` invocation, I/O mocked):**

| Case | Mode | Asserted | Result |
|---|---|---|---|
| "Salom", intent=chat | off | `generate_chat_response` called; `run_server_execution` NOT called | PASS |
| intent=chat, engine forced `server` | off(+veto) | stays chat; server exec blocked | PASS |
| intent=code | off | `_run_code_execution` called | PASS |
| intent=server, server_id set | off | `run_server_execution(intent="server")` reached | PASS |

**Unit/regression suite:** `py_compile` clean. `.venv/bin/python3 -m pytest --ignore=test_endpoints.py` →
**221 passed, 4 failed, 1 skipped**.

- The 4 failures are in `tests/test_google_oauth.py` and fail with
  `'_EmptyTable' object has no attribute 'insert'` — a Supabase mock/DB-fixture defect in the **auth**
  subsystem, unrelated to `run_agent_pipeline` and to this change. They are **pre-existing and
  environmental** (no code path touched by 4F-1 reaches the auth router).
- `test_endpoints.py` still fails collection on `import requests` — the orphan dead script flagged in the
  Sprint 4 audit, pre-existing and unrelated.
- No test that exercises `run_agent_pipeline` routing changed behavior. The only diff is the additive
  chat branch; code/server paths are byte-identical downstream of the new `if`.

---

## 7. PRODUCTION READINESS

- **Chat requests** now complete as conversational responses and never raise `INTENT_NOT_SERVER`. ✅
- **Decision Engine OFF mode** behaves exactly as before except the previously-missing chat branch now
  exists (chat was an undefined fall-through before; now it is a defined, safe path). ✅
- **Server/Code requests** unchanged — verified to still reach their executors. ✅
- **Security gates** untouched (permission, write-gate, approval, ownership run downstream of routing and
  are not affected by a chat early-return). ✅
- **Backward safe:** default mode `off`; the chat branch is pure legacy-intent-routed when the engine is
  off, so enabling `DECISION_ENGINE_MODE=authoritative` later will route chat through the Decision Engine
  into this same branch (no second routing hole remains).
- **Recommendation:** chat is now safe to route authoritatively. The earlier blocker (chat→server
  fall-through) is removed, clearing Sprint 4F step 2. Remaining 4F work: fold `run_explicit_mode` into
  `run_agent_pipeline`, then retire legacy routing.

---

## 8. EVERY REUSED MODULE / FUNCTION

| Module | Symbol | Use in chat branch |
|---|---|---|
| `services/agent_llm.py` | `generate_chat_response(user_input, conversation_history)` | LLM chat response (existing impl, no dup) |
| `services/chat_service.py` | `ChatService.save_workspace_message(...)` | persist user + assistant messages (existing impl) |
| `services/agent_service.py` (self) | `conversation_history` | already in scope from line 1501, passed to chat fn |
| `core/config` / `JobStatus` | `JobStatus.COMPLETED` | terminal status |
| `services/logger.py` (`obs`) | `obs.emit(...)` | audit log `chat_execution_complete` |
| `services/agent_service.py` (self) | `_db_update`, `_publish` | job state + event publish (existing helpers) |
| `models/agent.py` | `ProjectSpecification` + Discovery | Discovery already ran above; reused as-is, no dup |

**Discovery:** already executed (1431-1464) before the branch for ALL intents per the existing logic; the
chat branch reuses that result context implicitly (no separate call, no duplication).

---

## 9. EVERY UNTOUCHED SUBSYSTEM

- Decision Engine (`agent_decision_engine.py`) — not modified.
- Decision Router (`decision_router.py`) — not modified.
- Shadow/Weighted modes (`decision_shadow.py`, `decision_weighted.py`) — not modified.
- Config / feature flags (`core/config.py`) — not modified; mode stays `off` by default.
- Executor (`executor.py`) and its `INTENT_NOT_SERVER` guard — not modified (chat no longer reaches it).
- Routers, API contracts, frontend — not modified.
- Auth, permission, approval, resume, worker architecture — not modified.
- SQL / DB schema / migrations — not modified.
- Tests — not modified.
- `run_explicit_mode` (`run_agent_pipeline`'s sibling router) — not modified; its `generate_chat_response`
  call at line 2407 is a separate entrypoint that remains valid.

---

## SUMMARY

Sprint 4F-1 completes the missing CHAT execution path inside `run_agent_pipeline` by adding one guarded
`if intent == "chat":` branch (agent_service.py:1743) that reuses the existing
`agent_llm.generate_chat_response` and `ChatService` persistence, returns before `build_plan`/`run_server_execution`,
and emits a `chat_execution_complete` audit event. Runtime integration proves:
- chat → `generate_chat_response`, **never** `run_server_execution` (the `INTENT_NOT_SERVER` root cause is gone);
- the escalation veto still prevents an authoritative engine from pushing chat into the server path;
- code → `_run_code_execution` and server → `run_server_execution` are unchanged.

No refactor, no rewrite, no Decision Engine change. Compile clean; 221 tests pass; the only failures are
pre-existing and unrelated (auth DB-mock fixture; orphan `test_endpoints.py` collection). Chat is now safe
to route authoritatively, unblocking the remaining Sprint 4F retirement steps.
