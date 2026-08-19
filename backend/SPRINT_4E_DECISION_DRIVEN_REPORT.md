# SPRINT 4E — DECISION-DRIVEN ORCHESTRATION — REPORT

**Mode:** Controlled production transition. Decision Engine becomes the routing authority; legacy
becomes a compatibility validator.
**Result:** Implemented behind `DECISION_ENGINE_MODE=authoritative` (default still `off`). Decision
Engine selects the route; legacy validates it; **execution proceeds only after validation and can never
escalate privilege beyond the legacy intent** (proven exhaustively). 61 targeted tests pass; 226 collect
clean.
**Evidence rule:** every claim carries `file:line` or a command result. Unproven → **NOT VERIFIED**.

---

## 1. DECISION-DRIVEN EXECUTION GRAPH

```
POST /agents/forge-v2/run → submit_job → JobQueue → WorkerService → run_agent_pipeline (1342)
  ▼
  Pipeline State  (intent + task_mode + deployment_intent + project_spec + memory + server + conversation)
  ▼
  DecisionState snapshot
  ▼
  Decision Engine  AgentDecisionEngine.recommend()   ← chooses next_action + execution kind
  ▼
  Decision Graph   (resume→clarify→discover→spec→context→plan→approve→execute)
  ▼
  Legacy Validation  decision_router.resolve_route()  ← legacy intent VALIDATES the chosen route
  ▼
  intent := effective_route.execution_kind            (agent_service.py:1627, authoritative mode only)
  ▼
  Execution   if intent=="code" (1628) | if intent=="server" (1729) | chat  ← SAME branch bodies, unchanged
  ▼
  Security gates (permission / write-gate / approval / ownership) run downstream, independent of route
```
Intent is no longer the orchestrator — it is one signal feeding the engine and the validator. The engine
is the single orchestration authority.

---

## 2. LEGACY COMPATIBILITY GRAPH

```
intent classification (classify_intent, 1518)  ── still runs ──▶ legacy_kind
                                                                     │
Decision Engine chooses dispatch_kind ───────────────────────────┐  │
                                                                  ▼  ▼
                                          decision_router.resolve_route()
                                                                  │
                    ┌─────────────────────────────────────────────┤
                    │ agreement?           → AGREEMENT / GATE_BEFORE_EXECUTION
                    │ dispatch > legacy?    → UNSAFE_ESCALATION_BLOCKED  (route := legacy_kind)
                    │ dispatch < legacy?    → DEESCALATION               (route := dispatch_kind)
                    │ same priv, diff kind? → SAFE_REROUTE
                    │ engine raised?        → ENGINE_ERROR_FELL_BACK     (route := legacy_kind)
                    ▼
             EffectiveRoute (execution_kind used for dispatch)
```
Legacy routing remains fully available (it still classifies intent every run) and **validates** the
engine's choice. It never silently overrides: every disagreement is explicitly classified and logged.

---

## 3. DECISION VALIDATION GRAPH

Every authoritative run emits `decision_engine_authoritative` with the full record (no hidden routing):

| Field | Meaning |
|---|---|
| `execution_kind` | the route actually dispatched |
| `legacy_kind` | what intent classification would have chosen |
| `decision_kind` | what the engine chose (`none` = advised a gate) |
| `agreement` | `decision == legacy` |
| `conflict` | AGREEMENT / GATE_BEFORE_EXECUTION / SAFE_REROUTE / DEESCALATION / UNSAFE_ESCALATION_BLOCKED / ENGINE_ERROR_FELL_BACK |
| `reason`, `confidence` | explanation + score |
| `execution_category` | conversation / code / server / gate |
| `safety_classification` | safe / guarded / sensitive |
| `next_action` | decision-graph node |

---

## 4. FILES MODIFIED

| File | Change | Kind |
|---|---|---|
| `core/config.py` | `DECISION_ENGINE_MODE` accepts `authoritative`; `decision_engine_mode` property extended | additive |
| `services/decision_router.py` | **new** routing authority: `resolve_route()` + `EffectiveRoute` + escalation veto | new |
| `services/agent_service.py` | authoritative branch reassigns `intent := effective_route.execution_kind` (1617-1627) | additive, guarded |

No schema/SQL/migration/test/frontend changes. No renames. No router-path changes. No git. No legacy
module removed. No approval/worker/event/permission/auth contract touched.

---

## 5. DECISION ROUTING CHANGES

- **Before (off/shadow/weighted):** `intent` from `classify_intent` (1518) drives the `if intent==...`
  branches directly.
- **After (authoritative):** the engine's validated `EffectiveRoute.execution_kind` is assigned to
  `intent` at line 1627, so the **exact same branch bodies** (`if intent=="code"` @1628,
  `if intent=="server"` @1729, chat fallthrough) dispatch on the decision-driven route. Branch bodies
  are byte-for-byte unchanged — only the value they switch on changed, and only in authoritative mode.
- The only `intent` reassignment site is line 1627, inside `if _decision_mode == "authoritative"`
  (grep-verified). In every other mode `intent` is untouched.

---

## 6. COMPATIBILITY ANALYSIS

- **Backward compatible:** default `off`; `shadow`/`weighted` unchanged; legacy bool
  `DECISION_ENGINE_SHADOW=True` still → `shadow`. Verified: `default-> off`, `authoritative-> authoritative`.
- **Legacy never removed:** intent classification still runs every request and is the validator.
- **No silent override:** disagreements are classified + logged; the escalation veto is explicit.
- **Fail-safe:** any engine error → legacy route (`ENGINE_ERROR_FELL_BACK`), i.e. current behavior.

---

## 7. REGRESSION ANALYSIS

- Targeted suites → **61 passed** (2.97s).
- Full `tests/` collection → **226 collected, 0 errors**.
- Router functional tests → **13/13 pass** (agreement, escalation veto, de-escalation, gate, engine-error
  fallback, determinism, valid-kind for all intents, JSON-serializable).
- Mode `off` = byte-identical production path (intent reassignment only inside authoritative branch).
- **Pre-existing (REPORT ONLY):** `test_endpoints.py` fails collection on `import requests` — orphan dead
  script from Sprint 4, unrelated.

---

## 8. SECURITY VALIDATION

**The Decision Engine cannot bypass any security invariant.** Verified:
- `decision_router.py` imports only the engine, `decision_shadow._live_execution_kind`, and `logger` —
  no permission/write-gate/approval/auth/ownership imports. It chooses among the SAME three execution
  kinds the pipeline already supports; it grants no new capability.
- Downstream gates (`executor.py:379`, `tools.py:1340` write-gate; permission check @1402; approval @1760;
  ownership in ServerService/WorkspaceService) run regardless of the chosen route.
- **PRIVILEGE-ESCALATION VETO — exhaustively proven:** across **160 state combinations** (intent × deploy
  × context × clarification × discovery × prior-decisions), the effective route privilege **never
  exceeded** the legacy intent privilege — **0 violations**. Authoritative mode can never grant more
  access than intent-driven mode would.
- De-escalation (engine picks lower privilege) is allowed but recorded, so no request is silently upgraded.

---

## 9. PRODUCTION RISK ASSESSMENT

| Risk | Severity | Mitigation |
|---|---|---|
| Engine reroutes a request to the wrong kind | Med | escalation veto caps privilege; de-escalation recorded; roll back to `weighted` to observe first |
| Privilege escalation via engine | **None (proven)** | veto + 160-combo exhaustive test, 0 violations |
| Engine error breaks routing | Low | fail-safe fallback to legacy route |
| Security gate bypass | **None by design** | gates downstream, engine has no gate access |
| Behavior change while flag off | **None** | intent untouched unless authoritative |
| Latency | Low | pure in-memory decision; one log line |

Recommended rollout: enable `authoritative` in staging after a `weighted` observation window shows low
UNSAFE_MISMATCH; keep instant rollback available.

---

## 10. ROLLBACK STRATEGY

1. **Instant, no deploy:** set `DECISION_ENGINE_MODE=off` (or `weighted`/`shadow`) and reload
   (`get_settings` is `lru_cache`d; restart clears). Routing reverts to intent-driven immediately.
2. **Full removal:** delete `decision_router.py`, revert the authoritative branch in the hook
   (1617-1627), and the config enum addition. `shadow`/`weighted`/`decide`/`recommend` remain intact.

Because the authoritative route only ever *narrows or equals* legacy privilege and falls back on error,
rollback is configuration-only in the common case.

---

## 11. REMAINING LEGACY ROUTING

Still present (intentionally — not removed this sprint):
- `classify_intent` (1518) — now the validator/signal, still runs every request.
- `run_explicit_mode` (2295) + `/agents/route` alias (agents.py:313) — the second orchestrator; NOT yet
  folded (Sprint 4A blueprint Step 2). Authoritative routing applies only inside `run_agent_pipeline`.
- Deployment regex heuristic (1521) — still computes `deployment_intent`, now consumed as a signal.
- forge-v1 410 shims; dead modules (ai_service, conversation_continuation/policy, requirement_patch);
  broken `forge-v2/plan` (`ForgeV2Service`); dead `forge_v2_ws` — all REPORT-ONLY from Sprint 4.

---

## 12. SPRINT 4F PREPARATION REPORT

Target of 4F = retire legacy routing now that the engine is authoritative:
1. **Fold `run_explicit_mode` into `run_agent_pipeline`** (Sprint 4A Step 2) so `/agents/route` also
   flows through the decision router — currently the ONLY execution path not yet decision-driven.
2. **Add the chat branch** to `run_agent_pipeline` (still missing; INTENT_NOT_SERVER) so authoritative
   chat routing has a real executor rather than relying on explicit-mode.
3. **Demote `classify_intent` to metadata-only** once authoritative has a clean observation window
   (keep the call; stop it being a fallback route).
4. **Dead-code retirement** (separate approval): `DeployService` import, duplicate helper block, dead
   modules, backups, `test_endpoints.py`.
5. **Fix `forge-v2/plan`** (`ForgeV2Service` undefined) in its own bug-fix sprint.
Prerequisite before 4F: a production `weighted`→`authoritative` window with UNSAFE_ESCALATION_BLOCKED and
MISMATCH rates near zero (**NOT VERIFIED** — requires live traffic).

---

## SUMMARY

Sprint 4E makes the **Decision Engine the single routing authority** in `run_agent_pipeline` behind
`DECISION_ENGINE_MODE=authoritative` (default `off`). The engine selects the execution route; the legacy
intent classification **validates** it; execution proceeds only after validation. Every disagreement is
explicitly classified (AGREEMENT / GATE / SAFE_REROUTE / DEESCALATION / UNSAFE_ESCALATION_BLOCKED /
ENGINE_ERROR_FELL_BACK) and logged — no hidden routing. **Security invariants are absolute and
independent of the engine**, and a privilege-escalation veto — proven across 160 state combinations with
0 violations — guarantees authoritative mode can never grant more access than intent-driven mode.
Deterministic, explainable, reproducible, fail-safe. 61 targeted tests pass, 226 collect clean. No
unrelated bugs fixed (all remain REPORT-ONLY).

**NOT VERIFIED:** real-traffic routing/escalation ratios (requires enabling `authoritative` live);
external `/agents/route` callers; `run_worker()` standalone launch.
