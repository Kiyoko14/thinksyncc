# SPRINT 4D — WEIGHTED DECISION MODE — REPORT

**Mode:** Production migration, strict safety. Recommend-only promotion of the Decision Engine.
**Result:** Engine promoted SHADOW → WEIGHTED behind a tri-state flag. **Execution unchanged; legacy
orchestration remains authoritative; no security gate touched.** All new logic verified; 61 targeted
existing tests pass; full `tests/` suite collects 226 clean.
**Evidence rule:** every claim carries `file:line` or a command result. Unproven → **NOT VERIFIED**.

---

## 1. WEIGHTED DECISION ARCHITECTURE

```
run_agent_pipeline (agent_service.py:1342)
  ... intent + task_mode + deployment_intent + project_spec + memory (already collected) ...
  ▼
  settings.decision_engine_mode ∈ {off, shadow, weighted}     # core/config.py (NEW property)
  ▼ (weighted)
  DecisionState snapshot  ──▶  AgentDecisionEngine.recommend(state)  ──▶  Recommendation (pure)
  ▼
  Agreement → Compatibility → Safety  (decision_weighted._classify)
  ▼
  classification ∈ {MATCH, SAFE_MISMATCH, UNSAFE_MISMATCH, BLOCKED, UNKNOWN}
  ▼
  WEIGHTED_STATS.record(...)  +  obs.emit("decision_engine_weighted", {...})   # record only
  ▼
  if intent == "code": ...        # LEGACY EXECUTION — UNCHANGED, AUTHORITATIVE
```

| Artifact | File | Role | Effects |
|---|---|---|---|
| Weighted engine surface | `services/agent_decision_engine.py` (extended) | `recommend(state) → Recommendation`; `Recommendation`, `SafetyLevel`, `ExecutionCategory` | **NONE** — pure/deterministic |
| Weighted harness | `services/decision_weighted.py` (new) | classify + stats + record | one `obs.emit` + in-proc counter; never raises |
| Tri-state flag | `core/config.py` (extended) | `DECISION_ENGINE_MODE` + `decision_engine_mode` property | config only |
| Pipeline hook | `services/agent_service.py` (edited, same block) | dispatch off/shadow/weighted | none when off |

---

## 2. DECISION RECOMMENDATION LIFECYCLE

1. **Snapshot** — pipeline builds an immutable `DecisionState` from already-collected signals (no new fetch).
2. **Recommend** — `AgentDecisionEngine.recommend()` calls the pure `decide()` and wraps it with weighting fields.
3. **Classify** — `_classify()` runs Agreement → Compatibility → Safety against the live intent-derived kind.
4. **Record** — increment `WEIGHTED_STATS`, emit one structured log line.
5. **Execute** — legacy pipeline proceeds exactly as before; the recommendation is never consulted for control flow.

Every step is deterministic and reproducible (verified: `recommend(s) == recommend(s)`).

---

## 3. DECISION WEIGHTING MODEL

`Recommendation` (frozen dataclass) fields — every recommendation is explainable:

| Field | Meaning |
|---|---|
| `decision` | the underlying pure `Decision` (next_action, execution_kind, required_modules, …) |
| `confidence` | float, from intent confidence + execution heuristics |
| `priority` | 1 (highest: resume/clarify/approve) … 4 (chat); gates outrank execution |
| `reason` | human-readable justification |
| `evidence` | tuple of the concrete signals that fired (intent, deployment_signal, clarification_questions, spec confidence, is_resume, …) |
| `required_signals` | inputs the caller should have populated for this decision |
| `safety_level` | `safe` / `guarded` / `sensitive` — **advisory only** |
| `execution_category` | `conversation` / `code` / `server` / `gate` |

`safety_level` mapping: server/deploy → `sensitive`; code or approval-required → `guarded`; chat/no-write → `safe`.

---

## 4. SAFETY VALIDATION

**Security invariants never depend on the Decision Engine.** The engine has no reference to and never
calls: Permission Engine, Write Gate (`executor.py:379`, `tools.py:1340`), Approval contracts, Execution
Policy, Authorization, Workspace Isolation, or Server Ownership. Verified:
- The engine + both harnesses import only `services.logger`, `threading`, `logging`, and dataclasses —
  no auth/permission/executor/tool imports.
- `safety_level` is a metadata label used solely for classification/stats; it cannot grant or bypass
  anything (it is never read by any gate).
- Server/deploy recommendations are routed through PLAN/APPROVE **gates** first (category `gate`), so a
  weighted recommendation can never suggest jumping straight to a write path — verified by test
  `server->gate+sensitive`.

---

## 5. CONFLICT RESOLUTION MODEL

There is **no runtime conflict to resolve**: legacy always wins because the recommendation is never
executed. Disagreements are *classified and recorded*, not resolved:

| Classification | Meaning | Rule |
|---|---|---|
| **MATCH** | engine execution_kind == live intent kind | agreement |
| **SAFE_MISMATCH** | disagree, no write implication (chat↔code) | compatibility OK, safety OK |
| **UNSAFE_MISMATCH** | disagree AND a server/write path is involved, or safety=sensitive | flagged for review |
| **BLOCKED** | engine recommends a pre-exec gate (clarify/discover/plan/approve/resume) | recommendation blocks vs live execute |
| **UNKNOWN** | engine could not infer a concrete execution kind at an execute node | undecidable |

Stability tracked per-job: repeated vs flipped execution category → `decision_stability` + `conflict_frequency`.

---

## 6. LEGACY COMPATIBILITY REPORT

- **Backward compatible with 4C:** the legacy `DECISION_ENGINE_SHADOW=True` bool still resolves to
  `shadow` mode via `decision_engine_mode` (verified: `legacy bool-> shadow`). Existing 4C deployments
  are unaffected.
- **Default `off`** (verified: `default mode: off`) ⇒ current production behavior; engine not computed.
- **Unrecognized values clamp to `off`** (verified: `garbage-> off`) — fail safe.
- No public API/model/class/function renamed; no router path changed; no legacy execution removed; no
  compatibility layer removed. The 4C shadow module (`decision_shadow.py`) is untouched and still used
  in shadow mode.

---

## 7. DECISION STATISTICS

`WEIGHTED_STATS` (in-process, thread-safe, reset on restart) exposes via `snapshot()`:

| Metric | Definition |
|---|---|
| `total` | recommendations recorded |
| `counts` | per-class counts (MATCH/SAFE_MISMATCH/UNSAFE_MISMATCH/BLOCKED/UNKNOWN) |
| `recommendation_accuracy` | MATCH / total |
| `unsafe_mismatch_rate` | UNSAFE_MISMATCH / total |
| `decision_stability` | repeated / (repeated + flipped) per-job category |
| `repeated_decisions` | same category re-seen for a job |
| `conflict_frequency` | category flip-flops per job |

Recommendation accuracy, stability, repeated decisions, conflict frequency, unsafe recommendations, and
decision consistency (determinism) are all measured. Real-traffic values are **NOT VERIFIED** until the
flag is enabled in a live environment — this sprint provides the instrumentation.

---

## 8. PRODUCTION RISK ASSESSMENT

| Risk | Severity | Mitigation |
|---|---|---|
| Weighted computation adds latency | Low | pure in-memory; only when mode=weighted; sub-ms in test |
| Recommendation influences execution | **None by design** | recommendation never read for control flow; legacy authoritative |
| Security gate bypass | **None by design** | engine has no gate imports; safety_level is inert metadata |
| Harness exception leaks | Low | double-guarded try/except + harness swallows all + returns None |
| Log volume when weighted | Low | one INFO line/job on existing obs channel |
| Stats memory growth | Low | counters + one dict entry per job_id; bounded by active jobs; reset on restart |
| Flag misconfig | Low | invalid → off; legacy bool honored |

Net risk with mode=off (default): **effectively zero** — inert path.

---

## 9. ROLLBACK STRATEGY

1. **Instant, no deploy:** leave `DECISION_ENGINE_MODE=off` (default).
2. **Demote weighted→shadow→off:** change the env var and reload (`get_settings` is `lru_cache`d;
   restart clears). No code change.
3. **Full removal:** delete `decision_weighted.py`, revert the weighted branch in the pipeline hook, and
   the config additions. `decision_shadow.py` / engine `decide()` remain intact for 4C. No schema/API coupling.

---

## 10. FILES MODIFIED

| File | Change | Kind |
|---|---|---|
| `core/config.py` | +`DECISION_ENGINE_MODE` + `decision_engine_mode` property (tri-state, honours legacy bool) | additive |
| `services/agent_decision_engine.py` | +`Recommendation`, `SafetyLevel`, `ExecutionCategory`, `recommend()` (pure) | additive |
| `services/decision_weighted.py` | **new** weighted harness: classify + stats + record | new |
| `services/agent_service.py` | pipeline hook now dispatches off/shadow/weighted (same guarded block) | additive, guarded |

No schema/SQL/migration/test/frontend changes. No renames. No router-path changes. No git. No legacy
execution or compatibility layer removed. No unrelated modules touched.

---

## 11. PUBLIC INTERFACES AFFECTED

**None.** No router path added/changed/removed. No public class/function/model renamed. New symbols
(`Recommendation`, `SafetyLevel`, `ExecutionCategory`, `recommend`, `record_weighted_recommendation`,
`WEIGHTED_STATS`) are additive internal services. `DECISION_ENGINE_MODE` is an internal setting.

---

## 12. REGRESSION REPORT

- Targeted suites: `test_reliability_v2_worker`, `test_objective_validation`, `test_reliability_sprint`,
  `test_constitution` → **61 passed** (`pytest -q`, 2.97s).
- Full `tests/` collection: **226 tests collected, 0 errors**.
- New weighted-layer functional tests: **all pass** (determinism, weighting fields, category/safety
  mapping, MATCH / SAFE_MISMATCH / UNSAFE_MISMATCH / BLOCKED / UNKNOWN, stats, no-raise, JSON-serializable).
- Import graph OK: `core.config`, `agent_decision_engine`, `decision_shadow`, `decision_weighted`,
  `agent_service`, `routers.agents`, `main`.
- Mode resolution verified: default `off`; legacy bool → `shadow`; explicit `weighted`; garbage → `off`.
- Live execution branch byte-for-byte unchanged; hook sits strictly before it and only reads in-scope vars.

**Pre-existing failure (REPORT ONLY, not introduced):** root-level `test_endpoints.py` fails collection
with `ModuleNotFoundError: No module named 'requests'` — the orphan dead script flagged in Sprint 4,
unrelated to this sprint.

---

## SUMMARY

Sprint 4D promotes the Decision Engine to **WEIGHTED (recommend-only)** behind a tri-state
`DECISION_ENGINE_MODE` flag (off/shadow/weighted, default off, legacy-bool-compatible). The engine now
emits explainable `Recommendation`s (confidence, priority, reason, evidence, required signals, safety
level, execution category), classified via Agreement → Compatibility → Safety into
MATCH/SAFE_MISMATCH/UNSAFE_MISMATCH/BLOCKED/UNKNOWN with running statistics. **The engine still never
executes, never overrides production, and never touches a security gate.** 61 targeted tests pass, 226
collect clean, and every recommendation is reproducible and deterministic. No unrelated bugs fixed (all
remain REPORT-ONLY from Sprint 4).

**NOT VERIFIED:** real-traffic classification ratios (requires enabling `weighted` in a live
environment); external `/agents/route` callers; `run_worker()` standalone launch.
