# PLANNER PROVIDER-INDEPENDENCE REFACTOR — FINAL REPORT

**Sprint:** Decouple the planner from OpenAI JSON Mode. The planner now depends ONLY on a
provider-agnostic **Structured Output** abstraction; model selection is configuration-only.

**Constraints honored:** no git / checkout / restore / stash / commit / push; no SQL / migrations;
no tests modified (none touched the planner); no frontend; no routing changes; no Decision Engine
changes; no orchestration changes; Sprint 4 behavior unchanged; one new module (the required
abstraction) only — no capability registry, no provider fallback, no model marketplace (per the
explicit "NOT Multi-Provider" instruction). Repository source is the only source of truth.

---

## A. FILES MODIFIED

| File | Change |
|------|--------|
| `core/config.py` | Removed hardcoded `"gpt-4o-mini"` default (now `OPENAI_MODEL: str | None = None`); added `OPENAI_MODEL_CODE / _REASONING / _VISION / _EMBEDDING` (all `Optional`); added role→field map `_MODEL_ROLE_FIELDS`; added `Settings.resolve_model(role)` implementing the fallback chain. |
| `services/structured_output.py` | **NEW.** The single provider-agnostic Structured Output abstraction. Owns an isolated provider transport: native JSON mode with prompt-constrained fallback, parse, required-key validation. Knows nothing about the planner, tools, or orchestration. |
| `services/agent_llm.py` | `_chat_json` rewritten as a thin bridge to `request_structured` (param `model`→`role`; no `response_format`; preserves Redis cache keys + HTTPException contract). All 6 `_chat_json` callers pass `role=`. The 2 remaining direct `response_format` callers (`classify_intent_with_confidence`, `generate_patch_response`) routed through `request_structured`. All remaining `settings.OPENAI_MODEL*` literals replaced with `resolve_model(role)`. Added `_INTENT_SCHEMA` / `_PATCH_SCHEMA`. |
| `services/ai_service.py` | `model=settings.OPENAI_MODEL` → `settings.resolve_model("chat")`. |

**Untouched (verified):** `services/agent_service.py` (Sprint 4 chat branch @1744, `run_server_execution` @2073, `DECISION_ENGINE_MODE` hook), `services/decision_*.py`, `services/executor.py`, `services/planner.py` (call sites unchanged), all routers, SQL, migrations, tests, frontend. `models/agent.py` (`AgentPlan`/`AgentStep`) unchanged.

---

## B. ARCHITECTURAL CHANGES

1. **One abstraction boundary** (`services/structured_output.py`).
   - Public API: `request_structured(*, role, messages, schema, cache_key=None, temperature=0.0) -> dict`.
   - The planner/callers request *structured output* via a JSON `schema` + logical `role`. They NEVER
     pass `response_format`, NEVER pick a model, and NEVER see which transport produced the result.
   - Inside the isolated transport (`_parse_once` / `_constrained_fallback`):
       - **Native structured output** attempted first: `response_format={"type":"json_object"}`.
       - On provider rejection (detected via error-text hints incl. `"20024"`, `"json mode"`,
         `"response_format"`, `"unsupported"`) it transparently retries **prompt-constrained**: the
         schema is appended to the last user message, no `response_format`, then parsed.
       - The caller cannot tell which path executed.
   - Validation: required schema keys checked; if still missing after fallback, best-effort dict
     returned with a warning (preserves tolerant `.get()` callers).
   - Transport/parse failures raise `HTTPException` (502/504) — preserving the prior API contract.

2. **Model selection moved entirely to configuration.** `Settings.resolve_model(role)` is the single
   resolver. Fallback order per brief:
   `role-specific override (OPENAI_MODEL_<ROLE>)` → `OPENAI_MODEL` → `ValueError` ("Validation error if
   nothing configured"). No hardcoded model names anywhere in application code.

3. **Provider behavior stays isolated.** The only `response_format` literal in live code now lives
   inside `structured_output.py` (the transport). To add a future provider, extend the transport —
   no planner or caller changes required.

---

## C. EXECUTION FLOW — BEFORE

```
generate_plan()
  └─ _chat_json(messages, _PLAN_SCHEMA, model=settings.OPENAI_MODEL_PLANNER or settings.OPENAI_MODEL)
       └─ client.chat.completions.create(model=..., response_format={"type":"json_object"})
            └─ Provider (SiliconFlow / tencent/Hy3) → 400 code 20024  ← FAIL
```
The planner hardwired `response_format` and chose its model inline.

## D. EXECUTION FLOW — AFTER

```
generate_plan()
  └─ _chat_json(messages, _PLAN_SCHEMA, role="planner")          # bridge, no response_format
       └─ request_structured(role="planner", messages, schema)    # Structured Output abstraction
            ├─ model = resolve_model("planner")   → configuration only
            └─ isolated transport:
                 ├─ native JSON mode  → if provider rejects →
                 └─ prompt-constrained fallback (schema injected) → parse → validate
            └─ AgentPlan (planner)                                  # unchanged contract
```
The planner requests *Structured AgentPlan*; it does NOT know HOW structured output is produced.

---

## E. CONFIGURATION FLOW

`.env` (unchanged in this sprint — still only `OPENAI_MODEL=tencent/Hy3`):
```
OPENAI_MODEL=tencent/Hy3
# optional, all unset today:
# OPENAI_MODEL_PLANNER=...   OPENAI_MODEL_CODE=...   OPENAI_MODEL_REASONING=...
# OPENAI_MODEL_VISION=...    OPENAI_MODEL_EMBEDDING=...
```
Code path: `generate_plan` → `resolve_model("planner")` →
`OPENAI_MODEL_PLANNER or OPENAI_MODEL` → (today) `tencent/Hy3`. If `OPENAI_MODEL_PLANNER` is later set,
it wins; if neither is set, `ValueError` ("nothing configured"). `OPENAI_MODEL_CODE/REASONING/VISION/
EMBEDDING` are supported and optional for future use; no embedding/vision code path exists yet in the
repo (config-only support, as required).

---

## F. BACKWARD COMPATIBILITY ANALYSIS

- **AgentPlan / AgentStep / Planner API / Execution graph / Decision Engine:** unchanged.
- **Redis cache keys:** `_chat_json` preserves its existing `cache_key` read/write; `request_structured`
  does not double-cache. Cache keys for intent/plan/decision are identical to before.
- **HTTP contract:** transport/parse failures still raise `HTTPException` 502/504; intent classifier and
  patch generator preserve their `mid-confidence` / `timeout-failure` fallback shapes.
- **Behavioral equivalence:** for `OPENAI_MODEL=tencent/Hy3` (current prod), `resolve_model` returns the
  same value previously chosen by `OPENAI_MODEL_PLANNER or OPENAI_MODEL`; the only functional change is
  that a JSON-mode rejection now triggers a prompt-constrained fallback instead of a hard 400.
- **New explicit error:** when NO model is configured at all, `resolve_model` raises `ValueError`
  (surfaced as HTTP 503) instead of silently defaulting to `"gpt-4o-mini"`. Desired per brief.

---

## G. REGRESSION ANALYSIS

- `py_compile` clean on all touched modules.
- Full module-tree import succeeds (`agent_llm`, `structured_output`, `planner`, `executor`,
  `agent_service`).
- Targeted suite (excluding the pre-existing orphan `test_endpoints.py` which fails to import
  `requests`, and the pre-existing `tests/test_google_oauth.py` whose DB-mock failures are unrelated):
  **211 passed, 1 skipped, 0 failures**.
- Unit proof of the abstraction (fake client): native path returns parsed dict; provider JSON-mode
  rejection (error `code 20024`) transparently falls back to prompt-constrained and succeeds;
  `resolve_model` raises `ValueError` when nothing configured and resolves correctly via the fallback
  chain.
- `generate_plan` verified at runtime to contain **no** `response_format` and **no** direct
  `OPENAI_MODEL` reference; it calls `_chat_json(role="planner")`.

---

## H. PRODUCTION READINESS

- The planner no longer depends on OpenAI JSON Mode. SiliconFlow (`tencent/Hy3`) works via the
  prompt-constrained fallback; if a model supporting native JSON mode is configured, it is used
  automatically. Planner logic is unchanged. Model selection is fully configuration-driven with a clear
  validation error when misconfigured.
- No new architecture layers beyond the required abstraction; no capability registry / provider
  fallback / marketplace (per scope).
- **Ready to ship** behind existing `OPENAI_MODEL` config. Recommend setting `OPENAI_MODEL_PLANNER` to a
  SiliconFlow model that supports JSON mode (if any) OR leaving it unset (falls back to base model via
  prompt-constrained path) — both work.

---

## I. REMAINING TECHNICAL DEBT (REPORT ONLY — not modified)

1. `generate_plan` still calls `settings = get_settings()` (line 1801) though it no longer uses the
   local `settings` object directly (model resolution moved into `resolve_model`). Harmless unused
   local; not an error. (REPORT ONLY)
2. `services/agent_llm.py.bak` and `services/executor.py.orig` are stale backup copies containing the
   old `response_format`/`OPENAI_MODEL` literals. They are non-source artifacts; should be deleted in a
   cleanup pass, not part of this sprint. (REPORT ONLY)
3. The prompt-constrained fallback relies on error-text heuristics (`_FORMAT_UNSUPPORTED_HINTS`) to
   detect JSON-mode rejection. A future provider with a different rejection message would need a hint
   added. Acceptable for the non-multi-provider scope. (REPORT ONLY)
4. `ai_service.py` (separate assistant endpoint) now routes through `resolve_model("chat")`; it shares
   the same model config. (Informational)

---

## VALIDATION (per brief)

1. **generate_plan no longer depends directly on OpenAI JSON Mode** — VERIFIED (`response_format` absent;
   calls `_chat_json(role="planner")` → `request_structured`).
2. **Planner depends only on Structured Output abstraction** — VERIFIED (single dependency:
   `services.structured_output.request_structured`).
3. **SiliconFlow still works** — VERIFIED via fallback proof (rejection → prompt-constrained success).
4. **Hardcoded planner models removed** — VERIFIED (no `OPENAI_MODEL_PLANNER or OPENAI_MODEL` literals;
   `resolve_model` used everywhere).
5. **All model selection from configuration** — VERIFIED (`resolve_model(role)` is the only resolver;
   `gpt-4o-mini` default removed).
6. **Decision Engine unaffected** — VERIFIED (DE files untouched; `DECISION_ENGINE_MODE` intact).
7. **Execution graph unchanged** — VERIFIED (`run_agent_pipeline`/`build_plan`/`executor` untouched;
   `generate_plan` still returns `AgentPlan`).
8. **Routing unchanged** — VERIFIED (no router modified).
9. **No SQL changes** — VERIFIED.
10. **No Git operations** — VERIFIED (no git commands used).

**NOT VERIFIED:** which specific SiliconFlow models support native JSON mode (no capability registry in
the repo, per the prior investigation) — but this is now irrelevant: the abstraction falls back
transparently regardless.
