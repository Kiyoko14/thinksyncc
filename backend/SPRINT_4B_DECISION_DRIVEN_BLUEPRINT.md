# SPRINT 4B — DECISION-DRIVEN ORCHESTRATION (BLUEPRINT ONLY)

**Mode:** Architecture design + migration blueprint. **No code written, no patches, no file edits, no git.**
**Basis:** builds on `SPRINT_4_ARCHITECTURE_AUDIT.md` + `SPRINT_4A_CONSOLIDATION_BLUEPRINT.md`, re-verified this session.
**Thesis:** ThinkSync becomes decision-driven. The **Agent Decision Engine** orchestrates; **intent
becomes one weighted signal (metadata), not the router.**
**Evidence rule:** every claim carries `file:line`. Unproven → **NOT VERIFIED**.

---

## STEP 1 — COMPLETE ORCHESTRATION REVERSE-ENGINEERING

### Entry points (agent execution surface)
| Kind | File:Line | Notes |
|---|---|---|
| Primary run | `agents.py:119` forge_v2_run_async | → submit_job → JobQueue (async) |
| Poll | `agents.py:160` forge_v2_job_status | read-only |
| Plan | `agents.py:104` forge_v2_plan | **BROKEN — `ForgeV2Service` undefined** (report-only) |
| Legacy alias | `agents.py:313` explicit_intent_route | → `run_explicit_mode` (parallel orchestrator #2) |
| Event sources | `agents.py:204/237/269` reply/event/clarification-reply | → `EventWaitEngine.signal` |
| Live stream | `ws.py:57` job_ws | working WS (agents forge_v2_ws @176 is dead) |
| forge-v1 | `agents.py:44-99` | 410 GONE stubs |

### Orchestrators
- **#1 `run_agent_pipeline`** `agent_service.py:1342` — authoritative (forge-v2 path via worker).
- **#2 `run_explicit_mode`** `agent_service.py:2295` — legacy, single caller `/agents/route` (326); runs inline, duplicates classifier + code-exec.

### Planners
- `planner.build_plan` `planner.py:49` — the planner (server path @1704).
- `ImplementationIntelligence.decide_strategy` `implementation_intelligence.py` (called @agent_service:772) — strategy planner (patch/create) inside code path.

### Executors
- `_run_code_execution` `agent_service.py:672` — code intent (and explicit agent-mode @2393).
- `run_server_execution` `executor.py:256` → `_execute_with_lock` `executor.py:352` — server intent.
- `generate_chat_response` `agent_llm.py:416` (called @2341) — chat producer (explicit only).

### Approval path
`ApprovalPolicyEngine` (agent_service:1760) → `on_step_start` (1770) gate → `ApprovalSuspendSignal`
(models/agent.py:21) → caught @1986 → `approval_engine.py`.

### Resume path
resume detect `agent_service.py:1353` → `ResumeManager.load_resume_bundle` (1369) →
`transition_to_running` (1374) → re-enter pipeline at resume branch (1367).

### Event path
`EventWaitEngine.register` (2004) + `await_and_resume` detached task (2016) → parks on bus →
`signal` (event_wait_engine.py:227, from endpoints) wakes → `AgentService.run_job(bypass_semaphore=True)` (608).

### Context path
`ContextEngine.build_context` `agent_service.py:1029` (LIVE). `ProgressiveContextLoader.build`
(progressive_context.py:74/170, **wraps** ContextEngine) — ORPHAN, test-only importer.

### Constitution / safety gates (intent-coupled)
`ConstitutionEngine.check_job_state` (zombie gates, multiple), `constitution.py:1086` mode-based prompt
selection, `executor.py:379` + `tools.py:1340` hard `intent=="server"` write-gate.

---

## STEP 2 — INTENT-AS-PRIMARY-ROUTER OCCURRENCES (KEEP / DOWNGRADE / REMOVE / MERGE)

Every decision site where `intent` steers control (grep-verified):

| # | Site | Role | Decision | Proof / rationale |
|---|---|---|---|---|
| 1 | `agent_service.py:1518` classify_intent call | produces the routing key | **DOWNGRADE** to a *signal* fed into Decision Engine | it is the source of intent-routing |
| 2 | `agent_service.py:1519` coerce ∉{chat,code,server}→code | routing normalization | **DOWNGRADE** | becomes a signal-normalizer, not a router |
| 3 | `agent_service.py:1521-1527` regex deploy/telegram overrides | **heuristic re-routing** | **MERGE** into Decision Engine signal-weighting | brittle keyword routing → one decision input |
| 4 | `agent_service.py:1577` `if intent=="code"` branch | PRIMARY route | **DOWNGRADE** → decision "needs code execution?" | main split |
| 5 | `agent_service.py:1678/1690/1710` `intent=="server"` gating | PRIMARY route + context load | **DOWNGRADE** → decision "needs server plan/exec?" | server-only side effects |
| 6 | chat → *no branch* → server guard | routing GAP | **MERGE/FIX** (decision emits chat action) | INTENT_NOT_SERVER disconnect |
| 7 | `agent_service.py:2321` explicit auto `plan if chat else agent` | legacy route | **REMOVE** (folded via 4A Step 2) | duplicate of #1 in orchestrator #2 |
| 8 | `executor.py:379` `intent!="server"` → HTTPException | **safety write-gate** | **KEEP** (safety invariant, not orchestration) | prevents tool exec off server path |
| 9 | `tools.py:1340` `intent!="server"` block tool exec | **safety write-gate** | **KEEP** | same invariant, defense-in-depth |
| 10 | `planner.py:45/85/96` intent → tool/plan shape | plan construction | **DOWNGRADE** (planner reads decision, not raw intent) | plan flavor selection |
| 11 | `agent_llm.py:148/266/324/1802/2201` intent in prompt/tool selection | LLM prompt shaping | **KEEP as signal** | intent legitimately shapes prompts; not orchestration |
| 12 | `constitution.py:1086-1090` mode→prompt content | prompt selection | **KEEP** | prompt template, not routing |
| 13 | `requirement_discovery.py:902/1068` IntentType gating | discovery skip logic | **DOWNGRADE** → decision input | already a sub-decision |
| 14 | `conversation_continuation.py:168-233` ContinuationIntent | (DEAD module) | **REMOVE** (report-only; dead) | 0 importers |

**Summary:** KEEP = the two safety write-gates (#8,#9) + prompt-shaping (#11,#12). Everything that uses
intent to **choose a pipeline** is DOWNGRADE/MERGE. `classify_intent` stays alive but demoted to a signal.

> Note: `classify_intent_with_confidence` (`agent_llm.py:161`, already produces a confidence dict, used
> internally @229) is the natural seam to feed a *weighted* intent signal into the Decision Engine.

---

## STEP 3 — ONE DECISION ENGINE (design)

### Placement (non-invasive)
A new pure component `AgentDecisionEngine` sits **between** the signal-collection phase and the branch
selection inside `run_agent_pipeline`. It does **not** replace the pipeline; it replaces the
`if intent==...` cascade (sites #4,#5,#6) with a single `decision = DecisionEngine.decide(state)` call.

### Inputs (the DecisionState — NOT just intent)
| Input | Source (existing) |
|---|---|
| User Request | `payload.objective` |
| Intent signal (weighted) | `classify_intent_with_confidence` (agent_llm.py:161) |
| Task mode | `detect_task_mode` (agent_llm.py:251, called @1549) |
| Conversation | `ChatService.get_recent_context_messages` (@1504) |
| ProjectSpecification | `requirement_discovery.run_discovery` (@1444) |
| Requirements/needs-discovery | `should_run_discovery` (@1438) |
| Implementation Intelligence | `ImplementationIntelligence.decide_strategy` (impl_intel) |
| Context | `ContextEngine.build_context` → later `ProgressiveContextLoader` |
| Clarification | `AdaptiveClarificationEngine.evaluate` (adaptive_clarification.py:129) |
| Workspace | `WorkspaceService.get_workspace_by_id` |
| Repository | `RepositoryIndex` (repository_index.py) |
| Memory | `_memory_store.load` (@1516) + `ProjectBrain`/`ContextMemory` |
| Server State | `ServerService.get_server` + `detect_capabilities` |
| Previous Decisions | job `decisions` column + `DecisionMemory` |

### Output (a Decision, not a route)
```
Decision {
  needs_clarification: bool + questions        (AdaptiveClarification)
  needs_discovery: bool                         (RequirementDiscovery)
  needs_context: bool + depth                   (ProgressiveContext / ContextEngine)
  needs_repository: bool                        (RepositoryIndex)
  needs_specification: bool                      (ProjectSpecification)
  needs_planning: bool                          (planner.build_plan)
  action_kind: {chat | code | server}           (from weighted signals, NOT raw intent)
  needs_approval: bool                          (ApprovalPolicyEngine)
  needs_execution: bool
  needs_verification: bool
  needs_resume: bool                            (ResumeManager)
  confidence: float, rationale: [str]           (auditable)
}
```
`action_kind` reuses the SAME executors (`_run_code_execution`, `run_server_execution`,
`generate_chat_response`) — **no executor rewrite**, only the selector changes. Intent contributes a
weight to `action_kind`; workspace/spec/repository/history can override it (e.g. existing workspace +
patch spec → code even if intent leaned server). Safety write-gates (#8,#9) remain hard invariants.

---

## STEP 4 — ONE DECISION GRAPH (no hardcoded intent routing)

```
Understand (collect DecisionState: request + all signals)
  ▼
Need Clarification? ──yes──▶ AdaptiveClarification → suspend (EventWait) ──▶ Resume
  ▼ no
Need Discovery?     ──yes──▶ RequirementDiscovery → ProjectSpecification
  ▼
Need Context?       ──yes──▶ ProgressiveContext (wraps ContextEngine) / RepositoryIndex
  ▼
Need Specification? ──yes──▶ (spec completeness gate; low confidence → back to Clarification)
  ▼
Need Planning?      ──yes──▶ planner.build_plan (+ ImplementationIntelligence strategy)
  ▼
Decide action_kind  ── weighted(intent, spec, workspace, repo, history) → {chat|code|server}
  ▼
Need Approval?      ──yes──▶ ApprovalPolicyEngine → ApprovalSuspendSignal → EventWait → Resume
  ▼
Need Execution?     ──yes──▶ chat: generate_chat_response
                              code: _run_code_execution
                              server: run_server_execution → executor (write-gate KEEP)
  ▼
Need Verification?  ──yes──▶ deployment contract / step validation (executor _run_deployment_contract)
  ▼
Need Resume?        ──loop back via EventWaitEngine.await_and_resume
  ▼
Completed (worker _mark_completed / suspend-and-release)
```

The graph replaces the linear `intent→branch` cascade with conditional decisions, each backed by an
**already-existing** module. Intent only feeds the `action_kind` weighting node.

---

## STEP 5 — MODULES THAT NATURALLY BELONG INSIDE THE DECISION ENGINE

| Module | File | Role in DE | Exec order | Owns | Depends on | Required interface (target) |
|---|---|---|---|---|---|---|
| Requirement Discovery | requirement_discovery.py | discovery gate | 2 | spec generation | ProjectSpecification, LLM | `should_run_discovery`, `run_discovery` (exists) |
| Project Specification | models/agent.py | spec state | 2 | completeness/confidence | — | model (exists) |
| Adaptive Clarification | adaptive_clarification.py | clarification gate | 1 | question budget | ClarificationEngine (wraps), ClarificationBudget | `evaluate(...)` (exists) — needs **wiring** |
| ClarificationEngine | clarification_engine.py | internal review dep | 1 | review→question | — | `evaluate_review` (used @adaptive:480) |
| Progressive Context | progressive_context.py | context gate | 3 | layered context | ContextEngine (wraps), context cluster | `build(...)` (exists) — needs **wiring** |
| Context Engine | context_engine.py | file selection | 3 | PATCH/CREATE | — | `build_context` (exists) |
| Repository Index | repository_index.py | repo signal | 3 | incremental index | ContextEngine, workspace_files | index API (exists) |
| Project Brain / Context Memory / Knowledge Consistency / Self Evaluation | services/*.py | memory signals | 3 | durable knowledge | ContextMemory | (exist, orphan) |
| Implementation Intelligence | implementation_intelligence.py | strategy | 4 | patch/create decision | LLM, spec | `decide_strategy` (exists) |
| Planner | planner.py | plan build | 5 | plan bundle | templates, impl report | `build_plan` (exists) |
| Approval Engine / Policy | approval_engine.py, approval_policy.py | approval gate | 6 | suspend decision | Interactive/Event wait | (exists, wired) |
| Resume Manager | resume_manager.py | resume gate | 0/7 | cursor restore | DB | (exists, wired) |
| Event Wait Engine | event_wait_engine.py | suspend/wake bus | 6/7 | park+resume | Approval, InteractiveWait, Timeout | (exists, wired) |
| Conversation Continuation | conversation_continuation.py | **DEAD** | — | — | — | REPORT ONLY (0 importers) |

Approval/Resume/EventWait are **already** correctly wired — the Decision Engine *invokes* them, it does
not absorb them. Discovery/ImplIntel are already invoked — they become explicit decision nodes.
Progressive Context + Adaptive Clarification are the two ORPHANs that get *connected* as decision nodes.

---

## STEP 6 — MIGRATION PLAN (Current → Hybrid → Decision-Driven, no big-bang)

### Phase A — Consolidate (Sprint 4A prerequisites)
1. Add chat branch to `run_agent_pipeline` (closes INTENT_NOT_SERVER).
2. Fold `run_explicit_mode` behind the pipeline (`/route` = thin adapter).
3. Swap context call-site `1029` → `ProgressiveContextLoader`.
4. Wire `AdaptiveClarificationEngine`.
*(These make the NEW subsystems live and remove orchestrator #2 — done before any Decision Engine.)*

### Phase B — Hybrid (introduce DE alongside intent)
5. Add `AgentDecisionEngine.decide(state)` as a **pure, side-effect-free** component that consumes the
   already-collected signals and returns a `Decision`. **Shadow mode:** compute the Decision, LOG it,
   but keep the existing `if intent==...` cascade authoritative. Compare Decision.action_kind vs the
   intent branch in logs to build confidence (no behavior change).
6. Flip a feature flag so `action_kind` (not raw intent) selects the branch, while intent remains the
   dominant weight. Safety write-gates (#8,#9) unchanged.

### Phase C — Decision-Driven
7. Replace the intent cascade (sites #4,#5,#6) with the Decision Graph traversal; intent demoted to a
   weighted signal via `classify_intent_with_confidence`. Discovery/context/clarification/planning
   become explicit decision nodes.
8. Retire duplicated legacy routing (explicit auto @2321) once the DE is authoritative; keep public
   endpoints and 410 shims.

Each phase is independently shippable and reversible (flag-gated). No schema/API/test changes.

---

## REQUIRED OUTPUT SUMMARY

**1. Current orchestration graph** — Step 1 + Sprint 4 §2 (intent cascade → branch).
**2. Target Decision Graph** — Step 4.
**3. Decision Engine architecture** — Step 3.
**4. Module ownership** — Step 5 table.
**5. Execution ownership** — executors unchanged: chat=`generate_chat_response`, code=`_run_code_execution`, server=`run_server_execution`→executor; selected by `Decision.action_kind` instead of raw intent.
**6. Legacy ownership** — `run_explicit_mode` (fold), forge-v1 (410 keep), dead modules (ai_service, conversation_continuation/policy, requirement_patch — REPORT ONLY), duplicate helper block, dead WS/plan endpoints.
**7. Migration phases** — Step 6 (A/B/C).
**8. Risks** — below.
**9. Compatibility strategy** — below.
**10. Files that will eventually change** — below.
**11. Files that must NOT change** — below.
**12. Production migration roadmap** — below.

---

## 8. RISKS

| Risk | Severity | Mitigation |
|---|---|---|
| DE misclassifies action_kind vs today's intent branch | **High** | Phase B shadow-mode: log-and-compare before flipping; ship only when divergence understood |
| Activating ProgressiveContext + 7 modules adds runtime I/O (repo scans, Supabase) | **High** | feature-flag; offline/`StaleWorkspaceContext` guards; validate before Phase C |
| Adaptive clarification suspends jobs that never suspended before | Med | budget defaults (max=3, reserve=1) pure; reuse existing Approval/EventWait suspend infra |
| `run_explicit_mode` runs inline; folding changes lifecycle (inline→queued) | **High** | preserve `/route` contract; verify async 202 semantics with callers (NOT VERIFIED: external `/route` callers) |
| Safety write-gate accidentally downgraded with intent | **Critical** | #8/#9 explicitly KEEP as hard invariants — never fold into DE weighting |
| Monolith surface: `agent_service.py` (~2,481 lines) holds it all | Med | DE added as separate module; only the cascade region edited |
| Tests are off-limits but encode orphan-module behavior | Low | later phases rely on `test_sprint_3ce/3f1` as oracle without editing them |

## 9. COMPATIBILITY STRATEGY
- **No public rename:** `AgentService`, `run_agent_pipeline`, `run_server_execution`, `build_plan`,
  `ContextEngine`, `ClarificationEngine`, `EventWaitEngine`, executors — names frozen; DE is additive.
- **Endpoints frozen:** all router paths + 410 shims + `/route` kept (as adapter).
- **Intent preserved as data:** `intent` column + `classify_intent` remain; only their *authority* drops.
- **Flag-gated rollout:** shadow → weighted → authoritative; each reversible.
- **Executors untouched:** DE only changes *selection*, never execution bodies.
- **Safety invariants untouched:** `executor.py:379`, `tools.py:1340` stay hard gates.

## 10. FILES THAT WILL EVENTUALLY CHANGE
- `services/agent_service.py` — cascade region (1518-1710) → DE call; chat branch; explicit fold; context swap; clarification wiring.
- `routers/agents.py` — `/route` → thin adapter (no path change).
- `services/progressive_context.py` + cluster (context_memory, project_brain, repository_index, workspace_awareness, context_budget, knowledge_consistency, self_evaluation) — activated.
- `services/adaptive_clarification.py`, `services/clarification_budget.py` — wired; `clarification_engine.py` becomes internal dep.
- `services/agent_llm.py` — `classify_intent_with_confidence` used as the DE intent signal (no rename).
- **NEW:** `services/agent_decision_engine.py` (or similar) — the DE (additive; created in Phase B).

## 11. FILES THAT MUST NOT CHANGE (Sprint 4B = zero edits anyway; and beyond)
- **DB/schema/SQL/migrations:** `db/**`, `infra/**` — forbidden.
- **Tests:** `tests/**` — forbidden (migration oracle).
- **Frontend** — forbidden.
- **Reliability core (working):** `executor.py`*, `approval_engine.py`, `approval_policy.py`,
  `resume_manager.py`, `interactive_wait.py`, `event_wait_engine.py`, `timeout_manager.py`,
  `worker_service.py`, `job_queue.py`. (*executor safety gate line 379 must remain — do not weaken.)
- **Auth:** `google_auth.py`, `user_service.py`, `routers/auth.py`, `core/security.py`.
- **Gateway/edge:** `main.py` middleware, `routers/gateway.py`.
- **Constitution safety:** `agents/constitution.py` write/zombie gates.
- **Public models:** `models/**` names frozen (schema-bound).
- **forge-v1 410 shims** — keep.

## 12. PRODUCTION MIGRATION ROADMAP
```
Sprint 4A  → consolidate: chat branch, fold explicit, context swap, wire clarification   (orchestrator = ONE)
Sprint 4B  → THIS: design Decision Engine + Decision Graph (blueprint only, no code)
Sprint 4C  → implement AgentDecisionEngine (pure), SHADOW mode: compute+log Decision, intent still authoritative
Sprint 4D  → weighted mode: Decision.action_kind selects branch (flag on), intent = dominant weight
Sprint 4E  → decision-driven: replace intent cascade with Decision Graph; intent demoted to signal
Sprint 4F  → retire legacy routing (explicit auto), dead-code cleanup (separate approval); endpoints/410 kept
```
Every sprint is flag-gated, reversible, and independently shippable. No big-bang. Intent survives as
metadata; the Agent (Decision Engine) becomes the orchestrator.

---

## REPORT-ONLY — UNRELATED BUGS (not fixed, per STRICT RULES)
1. `/agents/forge-v2/plan` → `ForgeV2Service` undefined (`agents.py:113`) — runtime NameError.
2. `chat` intent → INTENT_NOT_SERVER (no chat branch) — resolved architecturally by Phase A Step 1.
3. `/agents/forge-v2/ws/{id}` dead (`agents.py:178` close 1008).
4. Dead import `DeployService` (agent_service:52); duplicate helper block (80-122); dead modules
   (ai_service, conversation_continuation, conversation_policy, requirement_patch); backups
   (`agent_llm.py.bak`, `executor.py.orig`); orphan `test_endpoints.py`.

---

*End of Sprint 4B blueprint. Zero source modified. Authoritative orchestrator target = AgentDecisionEngine
driving one Decision Graph; intent = weighted signal. NOT VERIFIED: external `/agents/route` callers,
`run_worker()` standalone launch, conversation_audit live invocation, dynamic imports of dead modules.*
