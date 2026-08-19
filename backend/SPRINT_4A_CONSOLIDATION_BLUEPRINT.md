# SPRINT 4A — ORCHESTRATION ARCHITECTURE CONSOLIDATION (BLUEPRINT ONLY)

**Mode:** Production architecture migration *planning*. **No source code modified, no patches, no fixes,
no routing edits, no merges, no deletions, no git.** Deliverable = evidence-backed migration blueprint.
**Basis:** builds on `SPRINT_4_ARCHITECTURE_AUDIT.md` (same tree, re-verified this session).
**Evidence rule:** every ownership decision carries a `file:line`. Unproven → **NOT VERIFIED**.

---

## 0. AUTHORITATIVE ARCHITECTURE DECISION (the single most important call)

> **The NEW architecture is authoritative. There is exactly ONE production orchestrator:
> `run_agent_pipeline` (`agent_service.py:1342`), reached only via the async worker queue.**

Proof:
- The only non-deprecated agent entry, `POST /agents/forge-v2/run` (`agents.py:119`), does **not** call
  `run_explicit_mode`. It enqueues (`agents.py:149 JobQueue.enqueue_job`) → `WorkerService.run`
  (`worker_service.py:92`) → `AgentService.run_job` (2282) → `_run_agent_loop` (2134) →
  `run_agent_pipeline` (1342).
- `run_explicit_mode` (`agent_service.py:2295`) has exactly **one caller**: the legacy alias
  `POST /agents/route` (`agents.py:326`). It is a *parallel* orchestrator, not part of the forge-v2 path.
- forge-v1 (`agents.py:44-99`) is already 410 GONE — the deprecation direction is settled.

Therefore the NEW pipeline **already owns** discovery, spec, planning, implementation intelligence,
approval, resume, and event-wait. The OLD constructs (Explicit/Plan/Server/Code "modes",
`run_explicit_mode`) are **compatibility layers to be folded into `run_agent_pipeline`**, not kept.

---

## 1. CURRENT ARCHITECTURE DIAGRAM (as-is, two partially-merged orchestrators)

```
                         ┌──────────────────────── ROUTERS ────────────────────────┐
POST /agents/forge-v2/run(119)   POST /agents/route(313)      POST /agents/forge-v2/plan(104)
        │  (LIVE, authoritative)        │ (LEGACY alias)              │ (BROKEN: ForgeV2Service undefined)
        ▼                               ▼                             ✗
 AgentService.submit_job(2145)   run_explicit_mode(2295)
        │                               │  mode=plan → generate_chat_response(2341)   ← ONLY real "chat"
 JobQueue.enqueue_job(149)              │  mode=agent → _run_code_execution(2393)     ← DUPLICATE exec entry
        ▼ (DB queue, async)            │  mode=auto  → classify → plan|agent
 WorkerService.run(92)                  └──────────────── PARALLEL ORCHESTRATOR #2 ───┘
        ▼
 run_agent_pipeline(1342)   ◀────────────── PRIMARY ORCHESTRATOR #1
   ├ resume check (1353)                    ResumeManager
   ├ permission (1402)
   ├ Requirement Discovery (1431)           requirement_discovery.run_discovery
   ├ classify_intent (1518) ─┬ code(1577) → _run_code_execution(672)
   │                         │                 ├ ImplementationIntelligence.decide_strategy(772)
   │                         │                 └ ContextEngine.build_context(1029)  ◀ OLD context in use
   │                         ├ server(1685) → build_plan(1704) → run_server_execution(1964)
   │                         │                                      └ executor._execute_with_lock(352)
   │                         └ chat → (NO BRANCH) → falls into server → INTENT_NOT_SERVER ✗
   └ ApprovalSuspendSignal(1986) → EventWaitEngine.register/await_and_resume → RETURN

   ORPHAN (built, NOT wired):  ProgressiveContextLoader(.build→wraps ContextEngine)  ·  AdaptiveClarificationEngine(wraps ClarificationEngine)
   DEAD (0 importers):         ai_service · conversation_continuation · conversation_policy · requirement_patch
```

Two orchestrators coexist: **#1 `run_agent_pipeline`** (authoritative) and **#2 `run_explicit_mode`**
(legacy). They **share** `_run_code_execution` but diverge on chat handling and job lifecycle.

---

## 2. TARGET ARCHITECTURE DIAGRAM (one orchestration model)

```
Client
  ▼
Router  (forge-v2/run = the only agent entry; /route folded in or kept as thin adapter → run_agent_pipeline)
  ▼
AgentService.submit_job → JobQueue → WorkerService → run_agent_pipeline   ◀── SINGLE ORCHESTRATOR
  ▼
Requirement Discovery      (requirement_discovery — unchanged)
  ▼
Project Specification      (models/agent.ProjectSpecification — unchanged)
  ▼
Planning                   (planner.build_plan — unchanged)
  ▼
Implementation Intelligence(implementation_intelligence — unchanged)
  ▼
Decision / Intent Routing  (ONE classifier → {chat, code, server} each with a REAL branch)
  ├ chat   → generate_chat_response          (chat branch added — folded from run_explicit_mode plan)
  ├ code   → _run_code_execution             (single exec entry; run_explicit_mode agent-mode folded in)
  └ server → build_plan → run_server_execution → executor
  ▼
Context                    (ProgressiveContextLoader.build — REPLACES direct ContextEngine call at 1029;
                            ContextEngine remains the internal file-selector it already wraps)
  ▼
Clarification              (AdaptiveClarificationEngine — wired at the discovery/plan gate;
                            ClarificationEngine remains its internal review dependency)
  ▼
Approval → Resume → Event Wait   (approval_engine / resume_manager / event_wait_engine — unchanged)
  ▼
Completion                 (worker _mark_completed / suspend-and-release — unchanged)
```

**Net change of the target:** ONE orchestrator, ONE exec entry per intent, chat has a real branch,
progressive context + adaptive clarification are *connected* (not rebuilt). No public class/function
renames, no router-path renames, no schema/API change.

---

## 3. MODULE OWNERSHIP TABLE

State: ACTIVE / PARTIAL / LEGACY / ORPHAN / DEAD. "Migration Required" = will this module be
*touched* during a later migration sprint (wiring or fold-in), not now.

| Component | File | State | Current Owner | Future Owner | Migration? | Proof |
|---|---|---|---|---|---|---|
| `run_agent_pipeline` | agent_service.py:1342 | ACTIVE | itself | **THE orchestrator** | YES (add chat branch, swap context call) | sole forge-v2 path |
| `run_explicit_mode` | agent_service.py:2295 | LEGACY | `/agents/route`:326 | folded into pipeline (kept as thin adapter) | YES | only caller = route alias |
| `_run_code_execution` | agent_service.py:672 | ACTIVE | pipeline + explicit | pipeline (single exec entry) | YES (dedupe entry) | called 1589 & 2393 |
| `AgentService.submit_job/create_job` | 2145/2161 | ACTIVE | routers | unchanged | NO | forge-v2/jobs routers |
| `WorkerService` | worker_service.py | ACTIVE | lifespan:170 | unchanged | NO | worker loop live |
| `JobQueue` | job_queue.py | ACTIVE | routers | unchanged | NO | enqueue in agents/jobs |
| Requirement Discovery | requirement_discovery.py | ACTIVE | pipeline:1431 | unchanged | NO | run_discovery wired |
| ProjectSpecification | models/agent.py:1912 | ACTIVE | discovery | unchanged | NO | used by discovery |
| Planner `build_plan` | planner.py:49 | ACTIVE | pipeline:1704 | unchanged | NO | server path |
| Implementation Intelligence | implementation_intelligence.py | ACTIVE | _run_code_execution:772 | unchanged | NO | decide_strategy called |
| **ContextEngine** | context_engine.py | ACTIVE | pipeline:1029 | **internal dep of ProgressiveContextLoader** | YES (call-site swap) | build_context @1029 |
| **ProgressiveContextLoader** | progressive_context.py | ORPHAN | tests only | **context owner** | YES (wire at 1029) | `.build`→wraps ContextEngine (170); importer = test only |
| context cluster (context_memory, project_brain, repository_index, workspace_awareness, context_budget, knowledge_consistency, self_evaluation) | services/*.py | ORPHAN | ProgressiveContextLoader | under ProgressiveContextLoader | YES (activated transitively) | reachable only from tests |
| **ClarificationEngine** | clarification_engine.py | PARTIAL (imported, uncalled) | agent_service:69 (unused) | **internal dep of AdaptiveClarificationEngine** | YES | evaluate_review reused @adaptive:480 |
| **AdaptiveClarificationEngine** | adaptive_clarification.py | ORPHAN | tests only | **clarification owner** | YES (wire into pipeline gate) | importer = test only; wraps ClarificationEngine |
| `clarification_budget` | clarification_budget.py | ORPHAN | tests only | under AdaptiveClarification | YES | test-only importer |
| Approval | approval_policy.py, approval_engine.py | ACTIVE | pipeline:1760 | unchanged | NO | ApprovalPolicyEngine wired |
| Resume | resume_manager.py | ACTIVE | pipeline:1369 | unchanged | NO | resume branch |
| Interactive Wait | interactive_wait.py | ACTIVE | pipeline:1822 | unchanged | NO | pause() wired |
| Event Wait | event_wait_engine.py | ACTIVE | pipeline:2004 + endpoints | unchanged | NO | register/signal/await_and_resume |
| Timeout Manager | timeout_manager.py | PARTIAL | event_wait_engine only | unchanged | NO | reachable via wait |
| Conversation Reliability | conversation_reliability.py | ACTIVE | main.py:126 (StartupVerifier) | unchanged | NO | startup verify |
| Conversation Audit | conversation_audit.py | PARTIAL | conversation_reliability | unchanged / review | NO | imported, invocation NOT VERIFIED |
| ConversationContinuation | conversation_continuation.py | DEAD | — | — (REPORT ONLY) | NO (do not remove now) | 0 importers |
| Conversation Policy | conversation_policy.py | DEAD | — | — (REPORT ONLY) | NO | 0 importers |
| Requirement Patch | requirement_patch.py | DEAD | — | — (REPORT ONLY) | NO | 0 importers |
| ai_service | ai_service.py | DEAD | — | — (REPORT ONLY) | NO | 0 importers |
| DeployService | deploy_service.py | LEGACY (dead import) | agent_service:52 (uncalled) | — (REPORT ONLY) | NO | imported never called |
| DeploymentService | deployment_service.py | ACTIVE | routers/deployments | unchanged | NO | live router |
| forge-v1 handlers | agents.py:44-99 | LEGACY | router (410) | unchanged (deprecation shim) | NO | 410 GONE |
| forge_v2_ws | agents.py:176 | DEAD | router (close 1008) | — (REPORT ONLY) | NO | unconditional close |
| `/v1/ws/jobs` | ws.py:57 | ACTIVE | frontend | unchanged | NO | working stream |
| forge_v2_plan / ForgeV2Service | agents.py:104/113 | DEAD/BROKEN | — | — (REPORT ONLY — unrelated bug) | NO | ForgeV2Service undefined |
| Gateway proxy | routers/gateway.py + main.py:331 | ACTIVE | host middleware | unchanged | NO | host-scoped dispatch |
| Duplicate helper block | agent_service.py:80-122 | DEAD | — | — (REPORT ONLY) | NO | redefined 128-170 |

---

## 4. EXECUTION GRAPH (authoritative NEW pipeline, verified transitions)

```
POST /agents/forge-v2/run (agents.py:119)
  → submit_job (2145)                     [why: create QUEUED job row]      competing? none for forge-v2
  → JobQueue.enqueue_job (149)            [why: durable async decoupling]   competing? BackgroundTasks (REMOVED — comment 147)
  → WorkerService.run (worker:92)         [why: single consumer]            competing? run_worker() standalone (worker:672 — NOT VERIFIED as launched; lifespan uses get_instance().run)
  → _claim_next_job (156) → _execute_job (232)
  → AgentService.run_job(bypass_semaphore=True) (2282)
  → _run_agent_loop (2134) → run_agent_pipeline (1342)
      → resume? (1353) ─ yes → ResumeManager.load_resume_bundle (1369)     competing? none
      → PermissionService.check_async (1402)                               competing? sync check in create_job (2164) — different phase, both intended
      → run_discovery (1444) if should_run_discovery (1438)                competing? none
      → classify_intent (1518)                                            competing? classify also in run_explicit_mode auto (2320) — LEGACY duplicate
      → code (1577) → _run_code_execution (672)                           competing? explicit agent-mode calls same fn (2393) — DUPLICATE ENTRY
      → server (1685) → build_plan (1704) → run_server_execution (1964)   competing? none
      → chat → MISSING branch → server guard INTENT_NOT_SERVER (executor:379)  competing? generate_chat_response only in run_explicit_mode (2341) — SPLIT
      → suspend (1986) → EventWaitEngine.register (2004) + await_and_resume task (2016) → RETURN
  → completion: worker _mark_completed (270) OR suspended-and-released
  → live events → Redis pub/sub → /v1/ws/jobs (ws.py:57)
```

Every transition above is a real call verified by grep/line. "competing" column names the OLD/parallel
transition and whether coexistence is intended.

---

## 5. LEGACY GRAPH (the parallel orchestrator to be folded in)

```
POST /agents/route (agents.py:313)
  → run_explicit_mode (agent_service.py:2295)
      → mode=auto  → classify_intent (2320) → plan|agent      [DUP of pipeline classifier @1518]
      → mode=plan  → generate_chat_response (2341) → {type:chat}   [the ONLY working chat producer]
      → mode=agent → create_job (2359) → _run_code_execution (2393)  [DUP exec entry; inline, NOT via worker queue]
```
Obsolescence proof: single caller is the legacy `/route` alias (marked "Legacy alias (hidden from docs)"
`agents.py:310`); it bypasses the worker queue (runs inline), duplicates the classifier and the code
executor, and is the only place chat works — which is exactly the capability the target pulls INTO
`run_agent_pipeline`.

---

## 6. ARCHITECTURE CONFLICTS (splits: KEEP / MERGE / REMOVE / NOT NOW)

| # | Split | OLD side | NEW side | Decision | Evidence |
|---|---|---|---|---|---|
| 1 | Orchestrator | `run_explicit_mode` (2295) | `run_agent_pipeline` (1342) | **MERGE** (fold explicit into pipeline; keep `/route` as thin adapter — no path rename) | only route caller; pipeline is forge-v2 path |
| 2 | Chat handling | `generate_chat_response` via explicit plan (2341) | missing chat branch in pipeline | **MERGE** (add chat branch in pipeline calling same fn) | INTENT_NOT_SERVER disconnect |
| 3 | Code exec entry | explicit agent-mode (2393) | pipeline code branch (1589) | **MERGE** (single entry) | same `_run_code_execution` |
| 4 | Intent classifier | explicit auto (2320) | pipeline (1518) | **MERGE** (one classifier) | duplicate calls |
| 5 | Context | `ContextEngine.build_context` @1029 | `ProgressiveContextLoader.build` (wraps it) | **MERGE** (swap call-site; ContextEngine stays as internal selector) | progressive wraps ContextEngine @170; superset return shape (docstring 24) |
| 6 | Clarification | `ClarificationEngine` (imported, uncalled) | `AdaptiveClarificationEngine` (wraps it) | **MERGE** (wire adaptive; ClarificationEngine = internal dep) | adaptive reuses evaluate_review @480 |
| 7 | Deploy | `DeployService` (dead import) | `DeploymentService` (live) | **REMOVE** import (REPORT ONLY now) | deploy_service imported never called |
| 8 | Job WS | `/agents/forge-v2/ws` (close 1008) | `/v1/ws/jobs` (live) | **REMOVE** dead route (REPORT ONLY now) | unconditional close |
| 9 | forge-v1 | 410 stubs | forge-v2 | **KEEP** (deprecation shims — do not remove) | intentional 410 |
| 10 | 3B conversation engines | continuation/policy/requirement_patch (dead) | conversation_reliability | **NOT NOW** (dead; decide after orchestrator merge) | 0 importers |
| 11 | Duplicate helper block | agent_service.py:80-122 | 128-170 | **REMOVE** (REPORT ONLY now) | shadowed redefinition |

---

## 7. MIGRATION ORDER (future sprints — NOT executed in 4A)

Ordered by dependency + risk (lowest-risk enabling steps first):

1. **Step 1 — Chat branch in `run_agent_pipeline`.** Add `if intent=="chat":` calling the existing
   `generate_chat_response`. Closes the INTENT_NOT_SERVER disconnect. No new module; reuses explicit logic.
2. **Step 2 — Fold `run_explicit_mode` into the pipeline.** Route `/agents/route` through
   `run_agent_pipeline` (keep the path + function name as a thin adapter). Removes orchestrator #2 and the
   duplicate classifier + duplicate code-exec entry. (Depends on Step 1 so chat still works after fold.)
3. **Step 3 — Context call-site swap.** Replace `ContextEngine.build_context` at `agent_service.py:1029`
   with `ProgressiveContextLoader().build(...)`. Activates the 3C.E/3F1 cluster. Superset return shape →
   downstream PATCH/CREATE branch untouched.
4. **Step 4 — Wire AdaptiveClarificationEngine** at the discovery/plan clarification gate; keep
   `ClarificationEngine` as its internal review dependency. Activates 3C.D.
5. **Step 5 — Dead-code retirement** (separate cleanup sprint, own approval): remove dead import
   `DeployService` (52), duplicate helper block (80-122), dead `forge_v2_ws` route, and — after
   confirming no reactivation intent — `ai_service.py`, `conversation_continuation.py`,
   `conversation_policy.py`, `requirement_patch.py`, `.bak`/`.orig`, `test_endpoints.py`.
6. **Step 6 — Fix `forge-v2/plan`** (`ForgeV2Service` undefined) — *tracked as an unrelated bug*, do in
   its own bug-fix sprint, not the consolidation.

Rationale for order: Steps 1→2 unify the orchestrator (highest architectural value, self-contained);
3→4 connect the already-built NEW subsystems (pure wiring); 5→6 are hygiene/bug and must not block the
consolidation.

---

## 8. RISK ANALYSIS

| Step | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 chat branch | chat now runs a different code path than the legacy `/route` | Med | route to the *same* `generate_chat_response`; behavior-identical |
| 2 fold explicit | explicit agent-mode runs **inline** today vs **worker queue** in pipeline → latency/lifecycle change | **High** | preserve `/route` contract; verify job lifecycle (QUEUED→RUNNING) parity; explicit "agent" becomes queued — confirm callers tolerate 202-style async |
| 3 context swap | ProgressiveContextLoader returns superset but downstream reads specific keys | Med | verified return shape is a superset (mode/selected_files/snippets/prompt_payload preserved, docstring 24); regression-test PATCH/CREATE branch |
| 3 context swap | activates 7 previously-dormant modules → new runtime + external calls (repository_index scans, Supabase workspace_files) | **High** | feature-flag the loader; validate offline/StaleWorkspaceContext handling |
| 4 clarification | adaptive engine may suspend jobs that previously never suspended | Med | budget defaults (max=3, reserve=1) already pure/no-I/O; gate behind existing approval/wait infra |
| 5 dead removal | a "dead" module may be reflectively/dynamically loaded | Med | grep dynamic import strings before delete; keep in a separate sprint |
| all | tests are OFF-LIMITS this sprint; some coverage lives only in test_sprint_3ce/3f1 for orphan modules | Low | migration sprints will need those tests as the wiring oracle |

**Cross-cutting risk:** `agent_service.py` is a ~2,481-line monolith holding both orchestrators; folding
explicit-mode in without a rename constraint requires surgical edits. NOT VERIFIED: whether any external
client calls `/agents/route` directly (frontend is off-limits to inspect here) — must be confirmed before Step 2.

---

## 9. FILES THAT WILL REQUIRE MIGRATION LATER

- `services/agent_service.py` — add chat branch (Step 1), fold explicit-mode (Step 2), swap context call-site (Step 3), wire adaptive clarification (Step 4). **Central migration surface.**
- `routers/agents.py` — `/route` becomes a thin adapter (Step 2); dead `forge_v2_ws` + broken `forge_v2_plan` addressed in later steps.
- `services/progressive_context.py` (+ its cluster: context_memory, project_brain, repository_index, workspace_awareness, context_budget, knowledge_consistency, self_evaluation) — activated transitively (Step 3).
- `services/adaptive_clarification.py`, `services/clarification_budget.py` — wired (Step 4); `services/clarification_engine.py` becomes an internal dependency (no rename).
- (Cleanup sprint) `services/ai_service.py`, `services/conversation_continuation.py`, `services/conversation_policy.py`, `services/requirement_patch.py`, `test_endpoints.py`, `.bak`/`.orig`.

## 10. FILES THAT MUST NOT BE TOUCHED DURING SPRINT 4A

**Everything.** Sprint 4A is blueprint-only — zero source edits. Beyond that, the following must not be
touched even in later migration steps unless a step explicitly names them:

- **Database/schema/SQL:** `db/schema.sql`, `db/migrations/*.sql`, `infra/supabase/*` — out of scope (STRICT RULES).
- **API contracts & router paths:** all `@router` paths and response models in `routers/*.py` — no renames.
- **Public classes/functions:** `AgentService`, `WorkerService`, `run_agent_pipeline`, `run_server_execution`, `build_plan`, `ContextEngine`, `ClarificationEngine`, `EventWaitEngine`, `ResumeManager`, `ApprovalPolicyEngine`, `DeploymentService` — names are frozen.
- **Reliability core (working, do not disturb):** `executor.py`, `approval_engine.py`, `approval_policy.py`, `resume_manager.py`, `interactive_wait.py`, `event_wait_engine.py`, `timeout_manager.py`, `worker_service.py`, `job_queue.py`.
- **Auth:** `google_auth.py`, `user_service.py`, `routers/auth.py`, `core/security.py`.
- **Gateway/edge:** `main.py` middleware (`route_workspace_hosts_to_gateway`, `normalize_collection_root_slash`), `routers/gateway.py`.
- **Tests:** `tests/**` — off-limits (they are the migration oracle for later wiring).
- **forge-v1 410 shims:** `agents.py:44-99` — intentional deprecation, keep.
- **Frontend, git, formatting, comments** — untouched per STRICT RULES.

---

## REPORT-ONLY: UNRELATED BUGS DISCOVERED (not fixed, per STRICT RULES)

1. **`POST /agents/forge-v2/plan` → runtime NameError:** `agents.py:113` calls undefined `ForgeV2Service`
   (`grep "class ForgeV2Service"` → none; `hasattr(routers.agents,"ForgeV2Service")` → False).
2. **`chat` intent → INTENT_NOT_SERVER:** no chat branch in `run_agent_pipeline`; falls to executor guard
   `executor.py:379`. (Resolved architecturally by Migration Step 1, but is a live bug today.)
3. **`/agents/forge-v2/ws/{id}` dead:** `agents.py:178` unconditional `close(1008)`.
4. **Dead import `DeployService`** (`agent_service.py:52`), **duplicate helper block** (80-122),
   **dead modules** (ai_service, conversation_continuation, conversation_policy, requirement_patch),
   **backup files** (`agent_llm.py.bak`, `executor.py.orig`), **orphan script** `test_endpoints.py`.

*(All above are REPORTED ONLY. No fixes applied. Consolidation blueprint contains no code changes.)*

---

*End of Sprint 4A blueprint. Zero source code modified. Authoritative architecture = NEW
(`run_agent_pipeline`); OLD Explicit/Plan/mode constructs are compatibility layers slated for fold-in,
not coexistence. NOT VERIFIED items: external `/agents/route` callers, `run_worker()` standalone launch,
conversation_audit live invocation, dynamic imports of "dead" modules.*
