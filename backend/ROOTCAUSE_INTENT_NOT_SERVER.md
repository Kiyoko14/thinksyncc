# STRICT ROOT-CAUSE INVESTIGATION — INTENT_NOT_SERVER with intent="chat"

**Method:** read-only source inspection. No patch, no git, no file/config/db/router/test changes.
**Verdict (one cause, code-proven):** **A — the Decision Engine never executed for this request**, because
`DECISION_ENGINE_MODE` resolves to `"off"` at runtime. Routing was therefore 100% legacy intent-driven,
and a **pre-existing legacy fall-through bug** (chat intent falls into the server executor) produced the
`INTENT_NOT_SERVER` error. Sprint 4E's authoritative branch is inert in production and played no part.

---

## 1. COMPLETE EXECUTION TRACE (this production request)

Objective: `"Serverning hostname va operatsion tizimini aniqla. Hech qanday o'zgartirish kiritma."`

```
run_agent_pipeline (agent_service.py:1342)
  settings = get_settings()                                   (1344)
  ...
  intent = classify_intent(objective) → "chat"                (1518)   # LLM classified chat
  intent not in {chat,code,server}? no                         (1519)
  deployment_intent = re.search(r"\b(deploy|server|app|run|website)\b", objective)  (1521)
        → False   (\bserver\b does NOT match "Serverning": 'server'+'n' = no word boundary)
  (deployment_intent False → lines 1522-1527 skipped; intent stays "chat")
  ▼
  _decision_mode = settings.decision_engine_mode → "off"       (1611)  # DECISION HOOK
  if _decision_mode in ("shadow","weighted","authoritative"):  (1612)  → FALSE
        → ENTIRE decision block (incl. `intent = _route.execution_kind` @1627) SKIPPED
  ▼
  if intent == "code":                                         (1643)  → FALSE (intent="chat")
  if intent == "server" and workspace_id:                      (1744)  → FALSE (intent="chat")
        → chat-specific setup skipped, BUT execution does NOT stop
  constitution_engine.check_objective(...)                     (1751)
  if intent == "server" and workspace_id:                      (1756)  → FALSE (context load skipped)
  plan_bundle = await build_plan(intent="chat", ...)           (1770)  # runs for ANY remaining intent
  ...
  try:                                                          (1985)
     loop_result = await run_server_execution(intent=intent="chat", ...)  (2030-2032)
        ▼
        executor.run_server_execution → _execute_with_lock(intent="chat")  (executor.py:321-323)
           normalized_intent = "chat"                           (executor.py:378)
           if normalized_intent != "server":                    (executor.py:379)
               raise HTTPException(INTENT_NOT_SERVER, intent="chat")   (executor.py:380)  ← ERROR
```

The pipeline has branches for `code` (1643, returns) and partial setup for `server` (1744/1756), but
**no branch and no early return for `chat`**. A `chat` intent falls straight through to the unconditional
`build_plan` (1770) + `run_server_execution` (2030) server path, which rejects it.

---

## 2. EXACT FILE & LINE WHERE ROUTING BREAKS

Two distinct facts, both required:

- **Why the Decision Engine didn't fix it:** `services/agent_service.py:1611-1612` —
  `_decision_mode = "off"` ⇒ the `if _decision_mode in (...)` guard is False ⇒ line **1627**
  (`intent = _route.execution_kind`) never runs.
- **Where the error is actually raised:** `services/executor.py:380` (guard at `:379`,
  `normalized_intent != "server"`), reached from the unconditional call at
  `services/agent_service.py:2030-2032` `run_server_execution(intent=intent, ...)`.

**First point where authoritative routing becomes ineffective:** `agent_service.py:1612` — the mode gate
is False, so the authoritative path is never entered.

---

## 3. PROOF FROM SOURCE CODE

**(a) Runtime mode = "off" (not just the default — the actual runtime value):**
- `core/config.py:93` → `DECISION_ENGINE_MODE: str = "off"`.
- `core/config.py:104-109` → `decision_engine_mode` returns `"off"` unless `DECISION_ENGINE_MODE` is
  `shadow/weighted/authoritative` OR the legacy `DECISION_ENGINE_SHADOW` bool is True.
- Repository scan: **no `DECISION_ENGINE_MODE=` and no `DECISION_ENGINE_SHADOW=` in any `.env`, shell,
  yaml, or toml file** (`grep -rn DECISION_ENGINE_MODE=` → none; `.env` present but has no such key).
  ⇒ Nothing overrides the default ⇒ runtime value is `"off"`.

**(b) The decision hook is gated by that value:**
- `agent_service.py:1611` `_decision_mode = getattr(settings, "decision_engine_mode", "off")`
- `agent_service.py:1612` `if _decision_mode in ("shadow", "weighted", "authoritative"):`
- `agent_service.py:1627` `intent = _route.execution_kind` — sits INSIDE that block, under
  `if _decision_mode == "authoritative"`. With mode `"off"`, this line is unreachable.

**(c) `intent` stays "chat" — every assignment after the hook:** grep of all `intent =` in
`run_agent_pipeline` shows the only post-classification reassignments are 1520/1523/1527 (deployment
heuristic, skipped here) and **1627 (inside the skipped decision block)**. No other code changes `intent`
before it reaches line 2032. So `intent == "chat"` at call time.

**(d) The objective never triggers the deployment heuristic:** `re.search(r"\b(deploy|server|app|run|
website)\b", "Serverning...")` → **no match**. `"Serverning"` = `server` + `n`; `\bserver\b` requires a
non-word char after `server`, so the word boundary fails. Hence line 1523 (`intent = "server"`) does not
fire, and intent remains the LLM's `"chat"`.

**(e) Executor reads the passed-in legacy intent, not the engine:**
- `agent_service.py:2032` passes `intent=intent` (="chat") into `run_server_execution`.
- `executor.py:256/321-323` forwards `intent=intent` into `_execute_with_lock`.
- `executor.py:378-380` normalizes that argument and raises `INTENT_NOT_SERVER` when it isn't `"server"`.
- The executor has **no reference to `AgentDecisionEngine`, `decision_router`, or `EffectiveRoute`**
  (`grep INTENT_NOT_SERVER` → only executor.py:380 and agent_llm.py:2204; executor imports no decision
  module). It only ever reads the intent string handed to it.

---

## 4. GUARD / BYPASS AUDIT (Step 7)

| Path | Active this request? | Evidence |
|---|---|---|
| mode = off | **YES** | config default, no env override |
| shadow path | no | mode≠shadow |
| weighted path | no | mode≠weighted |
| authoritative path (incl. `intent=_route...` @1627) | **no — skipped** | 1612 guard False |
| legacy fallback (decision_router `ENGINE_ERROR_FELL_BACK`) | no | router never called |
| exception fallback in hook (1639) | no | block not entered |
| code branch early return (1650/1677/1691/1742) | no | intent≠code |
| server branch guards (1744/1756) | no-op | intent≠server, but they don't stop flow |
| **chat: no branch, no early return** | **YES (the bug)** | falls through to 1770→2030 |
| executor guard (executor.py:379) | **YES (raises)** | normalized_intent="chat"≠"server" |

Only one path is active: **mode off → legacy intent controls routing → chat falls through to the server
executor → guard rejects it.**

---

## 5. WHICH ROOT CAUSE (Step 8)

- **A. Decision Engine never executed → CONFIRMED.** Mode is `"off"` (§3a/§3b), so the engine, the
  router, and line 1627 never ran.
- B. executed but result discarded → NO (never executed).
- C. executed but overwritten later → NO (never executed; and no assignment overwrites 1627 anyway).
- D. executor ignores Decision output → NO (executor never received any; it reads the legacy intent arg).
- E. legacy compatibility validator still controls routing → **effectively the observable symptom, but not
  via 4E's validator.** In mode off there IS no validator; plain legacy intent-driven routing is the only
  routing. The `decision_router` "legacy validator" is part of the authoritative path, which didn't run.
- F. other → the *error itself* is a **separate pre-existing legacy bug**: `run_agent_pipeline` has no
  `chat` branch, so chat intent reaches the server executor (this is the INTENT_NOT_SERVER disconnect
  documented in the Sprint 4 audit, independent of the Decision Engine).

**Single primary cause: A** (engine never executed because mode=off). **Contributing pre-existing
legacy defect (F):** missing `chat` branch in `run_agent_pipeline` lets chat fall through to the server
path — this is what actually raises the exception once the engine is absent.

---

## 6. WHY SPRINT 4E "PASSED" WHILE PRODUCTION FAILS

- The 4E verification ran the authoritative path **explicitly**: the isolated tests forced
  `DECISION_ENGINE_MODE="authoritative"` (config test) and the runtime proof set the mode on the settings
  object before invoking the pipeline. Production never sets that env var, so it runs mode `"off"`.
- The 4E report **stated this exactly**: "Implemented behind `DECISION_ENGINE_MODE=authoritative`
  (default still `off`)" and, in the follow-up runtime proof, "mode=off default — production untouched"
  and "authoritative chat routing is NOT complete until a chat branch is added (Sprint 4F) — another
  reason to keep it off." So 4E did not claim production was decision-driven; it claimed the branch works
  *when enabled*. The contradiction is between the mission's premise ("authoritative is the production
  authority") and the actual deployed config (mode off) — not a false 4E test.
- The runtime proof even reproduced this same fall-through: rerouting a job to `chat` caused it to skip
  the code branch and then fail in the server/SSH path — i.e. the missing-chat-branch defect was already
  visible and flagged.

**In short:** 4E tests passed because they enabled the flag; production fails because the flag is off AND
the legacy pipeline still has no chat branch.

---

## 7. MINIMAL ARCHITECTURAL FIX RECOMMENDATION (no code, no patch)

Two independent, minimal changes — in priority order:

1. **Add a `chat` branch (or explicit chat early-return) in `run_agent_pipeline`** before the
   unconditional `build_plan`/`run_server_execution` fall-through (between `agent_service.py:1742` and
   `:1770`). This fixes the actual `INTENT_NOT_SERVER` error for chat regardless of Decision Engine mode.
   It is the exact "add chat branch" item already listed as Sprint 4F step 2 and in the Sprint 4A/4B
   blueprints. This is the true root-cause fix for the raised exception.
2. **To make the Decision Engine actually control production routing**, set
   `DECISION_ENGINE_MODE=authoritative` in the production environment — but this MUST come *after* fix (1),
   because authoritative routing to `chat` would otherwise hit the same missing-chat-branch fall-through
   (as the 4E runtime proof demonstrated). Recommended sequence: fix chat branch → enable `weighted` to
   observe → enable `authoritative`.

Note also (report-only, do not change now): the deployment heuristic `\bserver\b` (agent_service.py:1521)
does not match Uzbek agglutinated forms like "Serverning", so even the legacy heuristic could not have
rescued this request into the server path. This is a separate signal-quality observation, not the root
cause.

---

## SUMMARY

The Decision Engine did **not** control this request because **`DECISION_ENGINE_MODE` resolves to "off"
at runtime** (no env override anywhere; `agent_service.py:1611-1612` gate is False, so line 1627 never
runs). Routing was purely legacy intent-driven; the LLM classified `"chat"`; the deployment regex didn't
match `"Serverning"`; and because `run_agent_pipeline` has **no chat branch**, the chat job fell through
to the unconditional server executor call (`agent_service.py:2030-2032`), which raised
`INTENT_NOT_SERVER` at `executor.py:380`. Root cause = **A** (engine never executed) + the pre-existing
**missing-chat-branch** legacy defect (F). Sprint 4E's tests passed only because they explicitly enabled
the flag; the branch is inert with the production default.
