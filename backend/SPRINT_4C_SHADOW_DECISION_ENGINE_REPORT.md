# SPRINT 4C — DECISION ENGINE IMPLEMENTATION (SHADOW MODE) — REPORT

**Mode:** Implementation. Production-safe, shadow-only, feature-flagged.
**Result:** Pure Decision Engine built + wired in shadow mode. **Live production behavior unchanged**
(flag defaults OFF). All new logic verified; 61 targeted existing tests pass; full `tests/` suite
collects 226 clean.
**Evidence rule:** every claim carries `file:line` or a command result. Unproven → **NOT VERIFIED**.

---

## 1. DECISION ENGINE ARCHITECTURE

Three artifacts, separation of concerns preserving purity:

| Artifact | File | Role | Effects |
|---|---|---|---|
| Pure engine | `services/agent_decision_engine.py` (new) | `AgentDecisionEngine.decide(state) → Decision` | **NONE** — pure/deterministic/stateless |
| Shadow harness | `services/decision_shadow.py` (new) | compute + compare + record MATCH/MISMATCH | exactly ONE effect: `obs.emit` log line; never raises |
| Feature flag | `core/config.py` (edited) | `DECISION_ENGINE_SHADOW: bool = False` | config only |
| Pipeline hook | `services/agent_service.py` (edited, 1 block) | flag-gated call to harness | none when flag OFF |

The engine consumes the **existing authoritative understanding** (Requirement Discovery, Project
Specification, Conversation, Memory, Implementation Intelligence, Progressive Context, Adaptive
Clarification, Workspace/Server, Approval/Resume/Event state, Intent confidence, History, current
execution state) via an immutable `DecisionState` snapshot. It introduces **no** Goal/Objective/
Mission/Task-State or any new orchestration layer, and it **replaces nothing** — it reads.

---

## 2. DECISION INTERFACE

```python
# services/agent_decision_engine.py
class AgentDecisionEngine:
    @staticmethod
    def decide(state: DecisionState) -> Decision: ...   # pure, deterministic, static
```
Stateless (all-static), no `__init__` state, no I/O. Input is a frozen `DecisionState`; output is a
frozen `Decision`. Same input → same output (verified: `d1 == d2`).

---

## 3. DECISION OBJECT SCHEMA

```python
@dataclass(frozen=True)
class Decision:
    next_action: NextAction          # clarify|discover|load_context|plan|approve|execute|verify|resume|complete
    execution_kind: ExecutionKind    # chat|code|server|none  (mirrors EXISTING intent domain — not a new concept)
    reason: str
    confidence: float
    required_modules: tuple[str, ...]
    approval_required: bool
    context_required: bool
    clarification_required: bool
    repository_required: bool
    specification_required: bool
    def to_dict(self) -> dict[str, Any]: ...
```
Matches the brief's required output fields (Next Action, Reason, Confidence, Required Modules, Execution
Kind, Approval/Context/Clarification/Repository/Specification Required). Frozen ⇒ immutable ⇒ no
downstream mutation.

`DecisionState` fields map 1:1 to signals already collected in `run_agent_pipeline` before the code
branch: `intent`, `intent_confidence`, `task_mode`, `has_specification`, `specification_confidence`,
`specification_missing_info`, `needs_discovery`, `conversation_len`, `memory_len`, `has_workspace`,
`has_server`, `context_available`, `clarification_pending/questions`, `approval_pending`, `is_resume`,
`interaction_state`, `deployment_signal`, `current_status`, `previous_decisions`.

---

## 4. DECISION LIFECYCLE

Deterministic graph traversal (Sprint 4B order), first matching gate wins:

```
resume? → clarify? → discover? → spec-confidence? → context? → plan(server)? → approve? → execute
```
Each gate returns a `Decision` with `next_action`, the responsible `required_modules`, and a reason.
Intent is only consulted inside `_infer_execution_kind` as a **weighted signal** (deployment signal +
workspace can adjust it) — never as the sole router.

---

## 5. SHADOW EXECUTION FLOW

```
run_agent_pipeline (agent_service.py:1342)
  ... classify_intent (1518) + detect_task_mode (1549) + deployment_intent (1521) + project_spec + memory ...
  ▼
  if settings.DECISION_ENGINE_SHADOW:            # agent_service.py ~1579 (NEW, flag-gated)
      DecisionState(...)  ← snapshot of already-collected signals
      record_shadow_comparison(job_id, trace_id, state)   → obs.emit("decision_engine_shadow", MATCH/MISMATCH/GATE)
  ▼
  if intent == "code": ...                        # UNCHANGED live execution continues
```
When the flag is OFF (default) the block is skipped entirely — zero cost, zero effect. The shadow call
is wrapped in `try/except` and the harness itself swallows all exceptions, so it can never perturb
production.

---

## 6. CURRENT vs DECISION COMPARISON

The harness maps the live pipeline's `intent` → execution kind (`_live_execution_kind`, coercing
unknown→`code` exactly as `agent_service.py:1519` does) and compares against
`Decision.execution_kind`:

| Live intent | Shadow execution_kind | Outcome |
|---|---|---|
| same as shadow | equal | **MATCH** |
| differs | differ | **MISMATCH** |
| any | `none` (engine advised a pre-exec gate: clarify/discover/approve/resume) | **GATE** |

Recorded via `obs.emit(level=INFO, layer="router", message="decision_engine_shadow", meta={...})` — the
existing structured observability channel (`services/logger.py`). No DB, no network.

---

## 7. MATCH / MISMATCH REPORT (verification results)

DB-free functional test (12 assertions) — **ALL PASS**:

| Check | Result |
|---|---|
| deterministic (`decide` idempotent) | PASS |
| Decision immutable (frozen) | PASS |
| server → PLAN | PASS |
| chat → EXECUTE(chat) | PASS |
| clarification gate wins | PASS |
| resume precedence | PASS |
| discovery gate | PASS |
| code-needs-context gate | PASS |
| shadow **MATCH** (intent=code) | PASS |
| shadow **GATE** (clarify → none) | PASS |
| live-kind map unknown→code | PASS |
| harness never raises (empty state) | PASS |

Production comparison data (real MATCH/MISMATCH ratios over live traffic) is **NOT VERIFIED** yet — it
is emitted to logs only when the flag is enabled in an environment with traffic; that is the observation
phase this sprint enables.

---

## 8. FILES MODIFIED

| File | Change | Kind |
|---|---|---|
| `core/config.py` | +`DECISION_ENGINE_SHADOW: bool = False` (+9 lines) | additive flag |
| `services/agent_decision_engine.py` | **new** pure engine (~330 lines) | new |
| `services/decision_shadow.py` | **new** shadow harness (~95 lines) | new |
| `services/agent_service.py` | +1 flag-gated shadow block after task_mode publish (~37 lines) | additive, guarded |

No other files touched. No schema/SQL/migration/test/frontend changes. No renames. No router-path
changes. No git.

Verification: `py_compile` OK on all four; import graph OK for `core.config`,
`services.agent_decision_engine`, `services.decision_shadow`, `services.agent_service`,
`routers.agents`, `main`.

---

## 9. PUBLIC APIs AFFECTED

**None.** No router path added/changed/removed. No public class/function/model renamed. The engine and
harness are internal services; the flag is an internal setting. `AgentService`, `run_agent_pipeline`,
executors, and all endpoints keep their signatures.

---

## 10. REGRESSION ANALYSIS

- Targeted existing suites: `test_reliability_v2_worker`, `test_objective_validation`,
  `test_reliability_sprint`, `test_constitution` → **61 passed** (`pytest -q`, 3.13s).
- Full `tests/` collection: **226 tests collected, 0 errors**.
- Flag default confirmed `False` at runtime (`get_settings().DECISION_ENGINE_SHADOW == False`) ⇒ the new
  code path is inert in production.
- Live execution branch (`if intent == "code": ...` and downstream) is byte-for-byte unchanged; the
  shadow block sits strictly *before* it and only reads variables already in scope (defined at
  1444/1463 `project_spec`, 1466 `server`, 1504/1511 `conversation_history`, 1516 `memory`, 1521
  `deployment_intent`).

**Pre-existing failure (REPORT ONLY, not introduced):** root-level `test_endpoints.py` fails collection
with `ModuleNotFoundError: No module named 'requests'`. It is the orphan script flagged as dead code in
the Sprint 4 audit, references nothing from this sprint, and is independent of these changes.

---

## 11. PRODUCTION RISK

| Risk | Severity | Mitigation |
|---|---|---|
| Shadow computation adds latency to the hot path | Low | pure in-memory logic, no I/O; only runs when flag ON; measured sub-ms in test |
| Shadow exception leaks into pipeline | Low | double-guarded: `try/except` at hook + harness swallows all + returns None |
| Log volume when flag ON | Low | one INFO line per job; existing obs channel |
| Engine drift vs live routing gives false MISMATCH | Med (observational only) | that IS the signal to collect; execution never follows the shadow decision in 4C |
| Flag accidentally enabled in prod | Low | even ON, effect = one log line; execution unchanged by design |

Net production risk with flag OFF (default): **effectively zero** — inert code path.

---

## 12. ROLLBACK STRATEGY

1. **Instant, no deploy:** leave `DECISION_ENGINE_SHADOW=False` (default) — the engine never runs.
2. **Disable after enabling:** set `DECISION_ENGINE_SHADOW=False` in env and reload settings
   (`get_settings` is `lru_cache`d; process restart clears it). No code change.
3. **Full removal (if ever):** delete the two new modules and the single guarded block in
   `agent_service.py` (~1579) + the config line. No schema/API/test coupling ⇒ clean revert.

Because every change is additive and flag-gated, rollback is configuration-only in the common case.

---

## SUMMARY

Sprint 4C delivers the **pure, deterministic, side-effect-free** Decision Engine from the Sprint 4B
design, running in **shadow mode** behind `DECISION_ENGINE_SHADOW` (default OFF). It **observes,
compares (MATCH/MISMATCH/GATE), and records** — it does **not** control execution. Production behavior
is unchanged; 61 targeted tests pass, 226 collect clean, and the only collection error is a
pre-existing, unrelated dead script. No unrelated bugs were fixed (all still REPORT-ONLY from Sprint 4).

**NOT VERIFIED:** real-traffic MATCH/MISMATCH ratios (requires enabling the flag in a live environment);
external `/agents/route` callers; `run_worker()` standalone launch.
