# STRICT READ-ONLY ARCHITECTURE AUDIT — generate_plan() → _chat_json() JSON PATH

**Method:** read-only source inspection. No file modified, no code, no patch, no config change, no git.
Repository files are the only source of truth.
**VERIFIED conclusion:** **C — Mixed.** The `generate_plan() → _chat_json()` path is a **legacy
OpenAI-specific implementation inherited from older development** (hardcoded `response_format=json_object`,
no capability awareness), NOT a deliberate current architectural requirement. However, the *planner
contract* (AgentPlan) genuinely needs structured output — which is an architectural requirement that
could be satisfied *without* the OpenAI-only `response_format` mechanism. The codebase is **mixed**: some
planner paths use `_chat_json`/JSON, one (`_build_simple_plan`) does not, proving JSON mode was never a
hard universal contract.

---

## 1. COMPLETE CALL GRAPH

```
run_agent_pipeline (agent_service.py:1342)
  └─ build_plan(intent, ...)                      (agent_service.py:1770 → services/planner.py:75)
       ├─ chat/code + simple  → _default_non_server_plan()      (planner.py:48)   [LOCAL, NO _chat_json]
       ├─ chat/code + complex → generate_non_server_plan(...)   (planner.py:108 → agent_llm.py:305)
       │                          └─ _chat_json(_NON_SERVER_PLAN_SCHEMA)  (agent_llm.py:339)
       ├─ server + simple      → build_simple_plan              (planner.py:131 → agent_llm.py:1571)
       │                          └─ _build_simple_plan()         (agent_llm.py:2038)  [LOCAL, NO LLM]
       └─ server + complex     → generate_plan(...)             (planner.py:135 → agent_llm.py:1772)
                                      └─ _chat_json(_PLAN_SCHEMA) (agent_llm.py:1824)   ◄── AUDIT TARGET

run_server_execution (agent_service.py:2073 → executor.py)
  ├─ executor.py:499  → agent_llm.generate_plan(...)            (agent_llm.py:1824)   ◄── AUDIT TARGET
  └─ agent_llm.py:2249 (internal run_server_execution) → generate_plan(...) (agent_llm.py:1824)
```
**Every caller of `generate_plan()`:**
- `services/planner.py:135` (`build_plan`, server+complex branch)
- `services/executor.py:499` (`run_server_execution`)
- `services/agent_llm.py:2249` (internal `run_server_execution` of `agent_llm`)
- (NOT called by the chat branch added in 4F-1 — `agent_service.py:1743` uses `generate_chat_response`)

**Every caller of `_chat_json()` (agent_llm.py:1703):**
- `agent_llm.py:287`  → `classify_intent_with_confidence` (intent classifier)  [_INTENT schema]
- `agent_llm.py:339`  → `generate_non_server_plan` (chat/code complex)          [_NON_SERVER_PLAN_SCHEMA]
- `agent_llm.py:404`  → `detect_task_mode` (task-mode classifier)               [_TASK_MODE schema]
- `agent_llm.py:1824` → `generate_plan` (server complex)                        [_PLAN_SCHEMA]   ◄── TARGET
- `agent_llm.py:1874` → `make_decision` / evaluate_step (debug agent)           [_DECISION_SCHEMA]
- `agent_llm.py:1935` → `revise_plan` (debug agent)                            [_PLAN_SCHEMA]

(`.bak`/`.orig` copies are non-source artifacts; excluded from the live graph.)

---

## 2. PLANNER DEPENDENCY GRAPH

```
generate_plan (agent_llm.py:1772)
  ├─ depends on: _chat_json (line 1824)                 [HARD dependency]
  ├─ depends on: _PLAN_SCHEMA (line 1579)               [JSON schema contract]
  ├─ depends on: AgentPlan / AgentStep (models/agent.py:220/210)  [output contract]
  ├─ depends on: constitution.build_prompt("planner")   (system prompt)
  └─ depends on: get_settings().OPENAI_MODEL_PLANNER or OPENAI_MODEL

_chat_json (agent_llm.py:1703)
  ├─ depends on: _get_openai_client()  (1646 → AsyncOpenAI(base_url=OPENAI_BASE_URL))
  ├─ depends on: response_format={"type":"json_object"}  (1726)   [HARDCODED]
  └─ depends on: json.loads(raw)  (1751)                 [RAISES if not JSON]

consumer of generate_plan output:
  build_plan (planner.py:135) → AgentPlan → run_server_execution (executor.py:499)
    → loop consumes plan.steps (typed AgentStep list)
```

---

## 3. JSON DEPENDENCY GRAPH

```
request carries response_format=json_object  (agent_llm.py:1726)  ← unconditional literal
        │
        ▼
SiliconFlow / OpenAI chat.completions.create
        │
        ▼
_chat_json enforces json.loads(raw) else HTTPException(502)  (agent_llm.py:1751-1756)  ← HARD FAIL
        │
        ▼
raw["steps"] / raw["context_summary"]  (agent_llm.py:1826-1840)
        │
        ▼
AgentStep(**s)  (agent_llm.py:1830)  ← requires dict shape matching AgentStep model

Same JSON enforcement at: agent_llm.py:196 (intent), :1144 (decision), :1726 (plan/non-server/decision)
```

---

## 4. HISTORICAL ARCHITECTURE INTERPRETATION

**Evidence the JSON path is legacy OpenAI-specific:**
- `_chat_json` hardcodes `response_format={"type":"json_object"}` (agent_llm.py:1726) — this is the
  **OpenAI-specific JSON mode** API field. SiliconFlow (the actual provider, `config.py:135`) does not
  support it for `tencent/Hy3` (proven in the prior planner JSON-mode root-cause investigation: `400
  code 20024`). A provider-aware architecture would not bake this literal in.
- The planner depends on `json.loads(raw)` and raises on failure (agent_llm.py:1751-1756). This couples
  planning to OpenAI-style guaranteed JSON — an assumption carried over from when `OPENAI_MODEL` defaulted
  to `gpt-4o-mini` (`config.py:136`), which *does* support JSON mode.
- The function header comment at `agent_llm.py:4` says: `generate_plan() — two-phase plan generation
  (used by forge_v2)` — "forge_v2" is the older codename, indicating this path originated in an earlier
  architecture generation.

**Evidence it is ALSO a genuine (if mis-scoped) contract need:**
- `generate_plan` returns `AgentPlan` (`models/agent.py:220`), whose `steps: list[AgentStep]` is consumed
  by the executor loop (executor.py:499 → step dispatch). The runtime needs *structured* steps, not free
  text. So "structured output" is a real requirement.
- BUT a structured result does NOT require `response_format=json_object`. The codebase already proves this:
  `_build_simple_plan` (agent_llm.py:2038) and `_default_non_server_plan` (planner.py:48) produce valid
  `AgentStep` plans **with no LLM and no JSON mode at all**. The executor loop accepts plans from both
  JSON and non-JSON sources. Therefore the *contract* is "parseable structured content," not "OpenAI JSON
  mode."

**Mixed-architecture proof (decisive):**
Within the *same* `build_plan` router (planner.py:75-135), three sub-paths coexist:
- `_default_non_server_plan` (local, no LLM) — planner.py:48
- `generate_non_server_plan` (`_chat_json`, JSON) — agent_llm.py:339
- `build_simple_plan` / `_build_simple_plan` (local, no LLM, no JSON) — agent_llm.py:1571/2038
- `generate_plan` (`_chat_json`, JSON) — agent_llm.py:1824
If JSON mode were a deliberate universal architectural pillar, the local paths would not exist, and
`build_simple_plan` would not bypass `_chat_json`. Their coexistence shows the JSON path was added for
the complex/LLM planner and never generalized — i.e., an inherited implementation detail, not a
top-level architectural decision.

**Age check:** the `.bak` snapshot (`agent_llm.py.bak:1768-1840`) of `generate_plan` is **byte-identical**
to the current version, including the `_chat_json(_PLAN_SCHEMA, ...)` call at `:1824`. This shows the
JSON dependency has been present across the available repository snapshots but provides **no** evidence of
when/how it originated (the `.bak` is a copy, not history). We cannot prove from the repo whether JSON was
"always" required or introduced later.
→ **NOT VERIFIED:** whether `generate_plan` *always* required JSON from the project's first commit (no VCS
  history available per audit constraints).

---

## 5. VERIFIED ANSWERS TO THE 8 INVESTIGATION POINTS

1. **Who calls generate_plan()?** `planner.py:135`, `executor.py:499`, `agent_llm.py:2249`. (§1)
2. **Why _chat_json instead of generic completion?** Because the planner needs a *structured* `AgentPlan`
   and the legacy design chose OpenAI `response_format=json_object` + `json.loads` to guarantee it
   (agent_llm.py:1724-1726, 1751). It is an implementation choice, not forced by the contract.
3. **Did it always require JSON / introduced later?** `agent_llm.py.bak` is identical (JSON present). No
   earlier non-JSON version exists in the audited artifacts. **NOT VERIFIED** (no VCS history).
4. **Planner contract: text or structured JSON?** Contract = **structured** (`AgentPlan`/`AgentStep`,
   models/agent.py:210/220). But satisfied by *parseable* content, not specifically OpenAI JSON mode —
   proven by `_build_simple_plan`/`_default_non_server_plan` which yield valid plans without it.
5. **Search hits:** `generate_plan` (agent_llm.py:1772, planner.py:135, executor.py:499, agent_llm.py:2249);
   `_chat_json` (def 1703; calls 287/339/404/1824/1874/1935); `response_format` (agent_llm.py:196/1144/1726);
   `json.loads` (1714/1751/202/1156/839/867/895/999/2359); `_PLAN_SCHEMA` (1579), `_NON_SERVER_PLAN_SCHEMA`
   (64), `_DECISION_SCHEMA` (1607); no `PlanSchema`/`PlannerSchema`/`ExecutionPlan`/`BuildPlan`/`PlanResult`
   /`StructuredOutput` symbols exist (those names are not used — schemas are `_*_SCHEMA`).
6. **Depends on guaranteed JSON or parseable structured?** `_chat_json` **requires guaranteed JSON** and
   raises 502 otherwise (agent_llm.py:1751-1756). The broader *contract* only needs parseable structured
   content. So the **function** depends on guaranteed JSON; the **architecture** does not require the
   OpenAI mechanism for it.
7. **If _chat_json disappeared, what breaks?** Exact modules:
   - `services/agent_llm.py` — `generate_plan` (1824), `generate_non_server_plan` (339),
     `classify_intent_with_confidence` (287), `detect_task_mode` (404), `make_decision`/`evaluate_step`
     (1874), `revise_plan` (1935) — all call `_chat_json` directly.
   - `services/planner.py` — `build_plan` (via generate_plan + generate_non_server_plan).
   - `services/executor.py` — `run_server_execution` (line 499 → generate_plan).
   - `services/agent_service.py` — `run_agent_pipeline` (→ build_plan → generate_plan).
   - Intent classification, task-mode classification, debug-agent decision/revise would also break (they
     share `_chat_json`). The chat branch (4F-1, agent_service.py:1743) does **NOT** use `_chat_json`.
8. **Architectural dependency or implementation detail?** `_chat_json` is an **implementation detail** that
   has become a *de-facto* shared utility (6 call sites). It is not an architectural boundary (no interface,
   no abstraction layer, no capability negotiation). Its JSON-mode literal is a legacy OpenAI assumption,
   not an architectural contract.

---

## 6. VERIFIED CONCLUSION

**C — Mixed.**

- The `generate_plan() → _chat_json()` JSON path is **legacy OpenAI-specific implementation** (hardcoded
  `response_format=json_object`, no provider capability awareness, origin noted as "forge_v2"). It is the
  proximate cause of the production `400 code 20024` failure on SiliconFlow.
- But the **planner contract legitimately requires structured output** (`AgentPlan`), which is an
  architectural requirement. That requirement, however, is already met by non-JSON code paths in the same
  module (`_build_simple_plan`, `_default_non_server_plan`), proving the *contract* does not depend on the
  OpenAI JSON mechanism.
- Therefore: **not a clean "architectural requirement" (A)** — because the architecture tolerates
  non-JSON plans; **not a clean "legacy implementation" (B)** — because structured output is genuinely
  needed. It is **mixed (C)**: a real structured-output need satisfied by an outdated OpenAI-only
  mechanism that should be either (a) made capability-aware (conditional `response_format`) or (b) replaced
  by prompt-constrained parsing + `json.loads` fallback (which `_chat_json` already does at 1751, except it
  raises instead of degrading when `response_format` is rejected upstream by the provider).

**NOT VERIFIED:** (i) whether `generate_plan` required JSON from the project's first commit (no VCS
history); (ii) which SiliconFlow models support JSON mode (no capability registry in repo — confirmed in
prior investigation).

**Every claim above cites file:line from the current repository. No file was modified.**
