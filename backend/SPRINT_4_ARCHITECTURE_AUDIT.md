# SPRINT 4 — ARCHITECTURE CONSOLIDATION AUDIT (READ-ONLY)

**Mode:** Read-only architecture investigation. No files modified, no patches, no git, no formatting.
**Scope:** `/root/thinksync/backend` (FastAPI + Supabase + Redis).
**Method:** direct source evidence — router registration, static import-graph reachability
(grep + AST), runtime import verification via the project venv
(`/root/thinksync/backend/.venv/bin/python3`), and runtime symbol checks. Every claim below
carries a file:line. Anything unproven is marked **NOT VERIFIED**.

---

## PHASE 1 — COMPLETE ENTRY POINT INVENTORY

### 1.1 Process bootstrap
| Entry | File:Line | Function | Caller | Downstream |
|---|---|---|---|---|
| ASGI app object | `main.py:196` | `app = FastAPI(...)` | uvicorn / `main.py:452` `__main__` | routers + middleware |
| Lifespan startup/shutdown | `main.py:108` | `lifespan()` | FastAPI on boot | diagnostics → http client → consistency check → APPROVAL_RESUME_SECRET gate → `StartupVerifier.verify` → spawns 3 background tasks |

### 1.2 Background workers / loops (spawned in `lifespan`)
| Entry | File:Line | Function | Caller | Downstream |
|---|---|---|---|---|
| Health-check loop | `main.py:135` | `run_health_check_loop()` | `asyncio.create_task` | `services/health_checker.py` |
| Recovery loop | `main.py:159` (`_recovery_loop`, defined 139) | `WorkerService.recover_stale_jobs` + `cleanup_dead_workers` | `asyncio.create_task` | `worker_service.py:557 / 627` (sync DB, run in executor) |
| **Worker loop** | `main.py:170` (`_worker_loop`, defined 165) | `WorkerService.get_instance().run()` | `asyncio.create_task` | `worker_service.py:92` → `_claim_next_job` → `_execute_job` → `AgentService.run_job` |

### 1.3 Scheduler
**NOT VERIFIED / NONE.** No cron, APScheduler, Celery-beat, or timed scheduler exists. The only
periodic work is the three `asyncio` loops above (fixed `asyncio.sleep` intervals). Grep for
`schedul`, `apscheduler`, `celery`, `crontab` → no production scheduler.

### 1.4 Telegram bot
**NOT VERIFIED as an execution entry point in this backend.** No aiogram / python-telegram-bot /
`Bot(` / long-polling / webhook handler is present. "Telegram bridge" appears ONLY in docstrings of
`routers/agents.py:212` (`post_job_reply`) and `:278` (`post_clarification_reply`) as a *described
future funnel*; the actual code path is the generic HTTP reply endpoints below. There is no Telegram
client in the codebase.

### 1.5 HTTP API endpoints (all `include_router` in `main.py:341-350`)
| Prefix | File | Endpoints (method path → function) |
|---|---|---|
| — | `routers/health.py` | `GET /health` (10) · `GET /metrics` (19) |
| `/auth` | `routers/auth.py` | `POST /auth/google` (41) · `GET /auth/me` (72) · `POST /auth/logout` (83) |
| `/servers` | `routers/servers.py` | `GET /servers/` (12) · `POST /servers/` (19) · `DELETE /servers/{id}` (27) |
| `/commands` | `routers/commands.py` | `POST /commands/execute` (15) |
| `/workspaces` | `routers/workspaces.py` | `POST` (12) · `GET /` (24) · `GET /{id}` (35) |
| `/chat` | `routers/chat.py` | `GET /chat/{workspace_id}` (14) · `POST /chat/{workspace_id}/message` (34) |
| `/deployments` | `routers/deployments.py` | `POST /{workspace_id}` (12) · `GET /{workspace_id}` (23) · `DELETE /{workspace_id}` (40) |
| `/agents` | `routers/agents.py` | see below |
| `/jobs` | `routers/jobs.py` | `POST /` (16) · `GET /` (37) · `GET /{id}` (46) · `/{id}/timeline` (62) · `/steps` (80) · `/decisions` (93) · `/retries` (106) · `/errors` (119) · `GET /recovery/report` (137) · `POST /recovery/{id}/mark-recoverable` (150) · `POST /recovery/{id}/mark-orphaned` (163) |
| `/v1/ws` | `routers/ws.py` | `WEBSOCKET /v1/ws/jobs/{job_id}` (57) |

**`/agents` endpoints (`routers/agents.py`):**
| Method Path | Line | Function | Status |
|---|---|---|---|
| `POST /agents/forge-v1/run` | 44 | `run_forge_v1` | **410 GONE** stub |
| `POST /agents/forge-v1/orchestrate` | 55 | `orchestrate_forge_v1` | **410 GONE** stub |
| `POST /agents/forge/plan` | 68 | `forge_plan` | **410 GONE** stub |
| `POST /agents/forge/run` | 79 | `forge_run_async` | **410 GONE** stub |
| `GET /agents/forge/jobs/{id}` | 91 | `forge_job_status` | **410 GONE** stub |
| `POST /agents/forge-v2/plan` | 104 | `forge_v2_plan` | **BROKEN — runtime NameError** (see Phase 4/7) |
| `POST /agents/forge-v2/run` | 119 | `forge_v2_run_async` | **LIVE — primary entry** |
| `GET /agents/forge-v2/jobs/{id}` | 160 | `forge_v2_job_status` | LIVE (poll) |
| `WEBSOCKET /agents/forge-v2/ws/{id}` | 176 | `forge_v2_ws` | **DEAD — immediately `close(1008)`** |
| `POST /agents/jobs/{id}/reply` | 204 | `post_job_reply` | LIVE (event source) |
| `POST /agents/jobs/{id}/event` | 237 | `post_job_event` | LIVE (event source) |
| `POST /agents/jobs/{id}/clarification-reply` | 269 | `post_clarification_reply` | LIVE (event source) |
| `POST /agents/route` | 313 | `explicit_intent_route` | LIVE (legacy alias → `run_explicit_mode`) |

### 1.6 Websocket entry points
- `routers/ws.py:57` `job_ws` — **the real, working live-event stream** (token auth → history → live pub/sub). This is the WS the frontend must use.
- `routers/agents.py:176` `forge_v2_ws` — **dead** (unconditional `await websocket.close(code=1008)`).

### 1.7 Internal service entry points (invoked by other services, not HTTP)
| Entry | File:Line | Invoked by |
|---|---|---|
| `AgentService.run_job` | `agent_service.py:2282` | `worker_service.py:267`, `event_wait_engine.py:608/728` |
| `run_agent_pipeline` | `agent_service.py:1342` | `_run_agent_loop` (2134) ← `run_job` |
| `run_server_execution` | `executor.py:256` | `agent_service.py:1964` |
| `EventWaitEngine.signal` | `event_wait_engine.py:227` | agents reply/event/clarification endpoints |
| `EventWaitEngine.await_and_resume` | `event_wait_engine.py:489` | `agent_service.py:2016` (detached task) |
| `Gateway proxy_request` | `routers/gateway.py` | `main.py:335` middleware (host-scoped), NOT a registered route |

---

## PHASE 2 — COMPLETE EXECUTION GRAPH

The single production execution path (agent job):

```
HTTP POST /agents/forge-v2/run            routers/agents.py:119 forge_v2_run_async
  ↓ AgentService.submit_job               agent_service.py:2145  (→ create_job:2161, inserts job status=QUEUED)
  ↓ JobQueue.enqueue_job(id)              routers/agents.py:149 → services/job_queue.py
  ↓ (returns 202; nothing runs inline)
──────────── async, decoupled via DB queue ────────────
Worker loop (lifespan)                    main.py:170 → WorkerService.run()  worker_service.py:92
  ↓ _claim_next_job                       worker_service.py:156 (atomic UPDATE queued→running)
  ↓ _execute_job                          worker_service.py:232 (+ _heartbeat_loop:301)
  ↓ AgentService.run_job(bypass_semaphore=True)  agent_service.py:2282
  ↓ _run_agent_loop                       agent_service.py:2134 (sets request mode)
  ↓ run_agent_pipeline                    agent_service.py:1342
      • resume check (WAITING_FOR_USER)   1353  → ResumeManager.load_resume_bundle
      • constitution/zombie gate          1386
      • PermissionService.check_async     1402
      • Requirement Discovery (Sprint 2)  1431  should_run_discovery / run_discovery
      • ServerService.get_server          1466
      • WorkspaceService.resolve/create   1480 / 1934
      • agent_llm.classify_intent         1518  → intent ∈ {chat,code,server}
      • agent_llm.detect_task_mode        1549
      • BRANCH intent=="code"             1577  → _run_code_execution (872-line fn @672)
                                                   → ContextEngine.build_context (1029)
                                                   → ImplementationIntelligence.decide_strategy (772)
                                                   → returns; job COMPLETED/FAILED (1669)
      • (server path, unconditional)      1685+ constitution.check_objective
              build_plan                  1704  services/planner.py:49
              on_step_start closure       1770  → ApprovalPolicyEngine (1760) + InteractiveWaitEngine.pause (1822)
              run_server_execution        1964  services/executor.py:256
                  ↓ _execute_with_lock    executor.py:352
                      GUARD intent!="server" → HTTPException INTENT_NOT_SERVER  executor.py:379-380
                      _run_validated_step / _exec_step / _evaluate loop  executor.py:578/738/790
  ↓ on ApprovalSuspendSignal (1986)  → EventWaitEngine.register(2004) + await_and_resume task (2016) → RETURN (worker released)
  ↓ else worker _mark_completed          worker_service.py:270
Live events → Redis pub/sub → WS         routers/ws.py:57 job_ws → _stream_live_events (22)
Resume path: POST /agents/jobs/{id}/reply → EventWaitEngine.signal (227) → parked await_and_resume wakes → AgentService.run_job(bypass_semaphore=True) (608) → run_agent_pipeline re-enters at resume branch (1367)
```

Node detail (incoming / outgoing):
- **forge_v2_run_async** (`agents.py:119`) — in: HTTP; out: `submit_job`, `JobQueue.enqueue_job`. Does NOT execute.
- **WorkerService.run** (`worker_service.py:92`) — in: lifespan task; out: `_claim_next_job`, `_execute_job`.
- **run_agent_pipeline** (`agent_service.py:1342`) — in: `_run_agent_loop`; out: discovery, planner, `_run_code_execution`, `run_server_execution`, EventWaitEngine.
- **_execute_with_lock** (`executor.py:352`) — in: `run_server_execution`; out: step loop; **hard `server`-only guard at 379**.
- **EventWaitEngine.await_and_resume** (`event_wait_engine.py:489`) — in: detached task; out: `AgentService.run_job` (608).

---

## PHASE 3 — PIPELINE INVENTORY

| Pipeline | Purpose | Entry | Exit | Status | Reachable? | Used? |
|---|---|---|---|---|---|---|
| **Forge-v2 run (Agent/Job)** | Queue+execute an agent job | `agents.py:119` | job COMPLETED/FAILED / WAITING_FOR_USER | LIVE | ✅ | ✅ primary |
| **Worker pipeline** | Consume DB queue, run jobs | `main.py:170` → `worker_service.py:92` | `_mark_completed/_failed/_abandoned` | LIVE | ✅ | ✅ |
| **Code pipeline** | `intent==code` → `_run_code_execution` | `agent_service.py:1577` | job result @1669 | LIVE | ✅ | ✅ |
| **Server/Execution pipeline** | `intent==server` → planner + executor | `agent_service.py:1685` → `executor.py:256` | `ToolCallingLoopResult` | LIVE | ✅ | ✅ |
| **Requirement Discovery** | Build ProjectSpecification pre-plan | `agent_service.py:1431` | `project_spec` attached | LIVE | ✅ | ✅ (gated by `should_run_discovery`) |
| **Planning pipeline** | LLM/template plan | `planner.py:49 build_plan` @1704 | `plan_bundle` | LIVE | ✅ | ✅ (server path only) |
| **Implementation Intelligence** | Strategy decision (patch/create) | `agent_service.py:772` | `intel_report` | LIVE | ✅ | ✅ (inside `_run_code_execution`) |
| **Context pipeline (ContextEngine)** | File selection PATCH/CREATE | `agent_service.py:1029` | `context_bundle` | LIVE | ✅ | ✅ |
| **Approval pipeline** | Gate risky steps, suspend | `ApprovalPolicyEngine` @1760, `approval_engine.py` | suspend / proceed | LIVE | ✅ | ✅ |
| **Resume pipeline** | Wake + continue suspended job | `event_wait_engine.py:489/227` | `run_job` re-dispatch | LIVE | ✅ | ✅ |
| **Clarification pipeline** | Suspend for a question | `clarification-reply` @269 + `ClarificationEngine` | resume | **PARTIAL** | ⚠️ endpoint reachable; engine imported (agent_service:69) but **never invoked** | ❌ engine orphaned |
| **Explicit mode pipeline** | mode=plan/agent/auto | `agents.py:313 /route` → `run_explicit_mode` (2295) | chat msg / job result | LIVE | ✅ | ⚠️ legacy alias, `plan` = only real `chat` route |
| **Chat pipeline** | Conversational reply | `agent_service.py:2340` (`generate_chat_response`) via `run_explicit_mode mode=plan` | `{type:chat}` | PARTIAL | ⚠️ only via `/route`, NOT via `/forge-v2/run` | ⚠️ (see Phase 4) |
| **Forge-v2 plan** | Plan without executing | `agents.py:104` | — | **DEAD/BROKEN** | ❌ runtime NameError | ❌ |
| **Forge-v1 (run/orchestrate/plan/jobs)** | legacy | `agents.py:44-99` | — | **DEAD** (410 GONE) | route exists, returns 410 | ❌ |
| **Progressive Context (3C.E/3F1)** | Layered context memory | `progressive_context.py` | — | **ORPHAN** | ❌ only tests import it | ❌ NOT wired to agent_service |
| **Adaptive Clarification (3C.D)** | Budgeted clarification | `adaptive_clarification.py` | — | **ORPHAN** | ❌ only tests import it | ❌ |
| **Conversation continuation / policy / requirement_patch (3B)** | Conversation reliability engines | resp. modules | — | **DEAD** | ❌ zero importers | ❌ |
| **AI service (pre-OAuth)** | legacy single-shot AI | `ai_service.py` | — | **DEAD** | ❌ zero importers | ❌ |
| **Gateway proxy** | Reverse-proxy workspace subdomains | `main.py:331` middleware → `gateway.proxy_request` | proxied response | LIVE | ✅ (host-scoped, not a route) | ✅ |

---

## PHASE 4 — ROUTING ANALYSIS

**Where intent is classified:** `agent_service.py:1518` `agent_llm.classify_intent`, then normalized:
- `1519` any value not in {chat,code,server} → forced `code`.
- `1521-1523` regex `\b(deploy|server|app|run|website)\b` in objective → forced `server`.
- `1524-1527` if `server` but objective matches `\b(telegram|bot|yoz|kod|code|python|script|program)\b` → back to `code`.

**Routing matrix (inside `run_agent_pipeline`, the production path):**
| Intent | Classified at | Routed at | Pipeline receiving | Executor | Terminates |
|---|---|---|---|---|---|
| `chat` | 1518 | **NO `if intent=="chat"` branch exists** | falls through past `code`(1577) into unconditional server path (1685+) | `run_server_execution` @1964 → `_execute_with_lock` | **`executor.py:379-380` raises `INTENT_NOT_SERVER` → job FAILED** ❌ DISCONNECT |
| `code` | 1518 | `1577 if intent=="code"` | `_run_code_execution` @672 | in-process code exec | COMPLETED/FAILED @1669 ✅ |
| `server` | 1518/1523 | 1685+ (unconditional after code) | `build_plan` → `run_server_execution` | `_execute_with_lock` (passes guard) | ToolCallingLoopResult ✅ |
| `plan` | n/a in pipeline | only via `/agents/route` `run_explicit_mode` (2295) | `generate_chat_response` (2341) | none | `{type:chat}` ✅ |
| `agent` | n/a in pipeline | only via `/agents/route` mode=agent (2344) | `_run_code_execution` (2393) | in-process | dict result ✅ |
| `unknown` | 1518 | 1519 → coerced to `code` | code pipeline | in-process | ✅ (never reaches server guard) |
| `show` | **NOT VERIFIED** | no classifier ever emits `show`; not in {chat,code,server}; would be coerced to `code` @1519 | code pipeline | in-process | ✅ (effectively unreachable label) |

### Routing disconnects (evidence)
1. **`chat` intent → `INTENT_NOT_SERVER` (CRITICAL).** `run_agent_pipeline` has branches for `code`
   (1577) and `server` (1685+) but **no `chat` branch**. A `chat`-classified job created via
   `/forge-v2/run` falls through to `run_server_execution` @1964 → `_execute_with_lock` @352 whose
   guard `executor.py:379-380` raises `HTTPException(INTENT_NOT_SERVER)`. The intended chat path
   (`run_explicit_mode` mode=`plan` → `generate_chat_response`) is reachable ONLY through the legacy
   `/agents/route` alias, which the `/forge-v2/run` flow never calls. *(Confirmed prior finding —
   `references/intent_routing_rca.md`.)* Mitigating factor: classifier heuristics (1521-1527)
   aggressively push objectives to `code`/`server`, so `chat` rarely survives — but when it does the
   job hard-fails.
2. **`POST /agents/forge-v2/plan` → runtime NameError (CRITICAL).** `agents.py:113` calls
   `ForgeV2Service.get_plan(...)` but `ForgeV2Service` is **imported/defined nowhere**
   (`grep "class ForgeV2Service"` → none; `hasattr(routers.agents,"ForgeV2Service")` → **False** at
   runtime). Module import succeeds (name only resolved at call time), so this is a latent
   `NameError` on every call. The endpoint is 100% broken.
3. **`WEBSOCKET /agents/forge-v2/ws/{id}` dead.** `agents.py:178` unconditionally
   `await websocket.close(code=1008)`. The functional WS is `/v1/ws/jobs/{id}` (`ws.py:57`) — a split
   between the advertised agent WS and the working one.
4. **Clarification engine orphaned.** `clarification-reply` endpoint (269) can wake a job, but
   `ClarificationEngine` (imported `agent_service.py:69`) is **never called** in the pipeline; adaptive
   clarification (`adaptive_clarification.py`) is imported only by tests. Clarification suspension is
   not actually initiated from the production path.

---

## PHASE 5 — SPRINT INTEGRITY

Legend: ✅ Implemented+connected · ⚠️ Partially connected · ❌ Disconnected/dead · 🔌 Only imported · 🧪 Only in tests

| Sprint / Feature | Module(s) | Status | Evidence |
|---|---|---|---|
| **S1 Core API/auth/servers/workspaces** | routers + services | ✅ | all routers included `main.py:341-350`, imports OK |
| **S1 Google OAuth** | `google_auth.py`, `user_service.py`, `auth.py` | ✅ | `auth.py` imports both; `POST /auth/google` live |
| **S2 Requirement Discovery** | `requirement_discovery.py` | ✅ | wired `agent_service.py:1431-1449` |
| **S2 ProjectSpecification** | `models/agent.py` | ✅ | imported `agent_service.py:1433`, used by discovery |
| **S2 Planner** | `planner.py` | ✅ | `build_plan` @1704 (server path) |
| **S2 Context Engine** | `context_engine.py` | ✅ | `build_context` @1029 |
| **S2 Implementation Intelligence** | `implementation_intelligence.py` | ✅ | `decide_strategy` @772 |
| **S3A Approval** | `approval_policy.py`, `approval_engine.py` | ✅ | `ApprovalPolicyEngine` @1760, `on_step_start` gate |
| **S3A Resume / ExecutionCursor** | `resume_manager.py` | ✅ | resume branch `agent_service.py:1353-1378` |
| **S3A Interactive Wait** | `interactive_wait.py` | ✅ | `InteractiveWaitEngine.pause` @1822 |
| **S3A.2/.3 APPROVAL_RESUME_SECRET gate** | `main.py:118`, `models/approval.py` | ✅ | fail-fast in lifespan |
| **S3B Startup Verification** | `conversation_reliability.py::StartupVerifier` | ✅ | `main.py:126-132` |
| **S3B Conversation Audit** | `conversation_audit.py` | ⚠️ | imported only by `conversation_reliability.py`; reachable but audit not called from live pipeline (NOT VERIFIED as invoked) |
| **S3B ConversationContinuation** | `conversation_continuation.py` | ❌ dead | **zero importers** (grep) |
| **S3B Conversation Policy** | `conversation_policy.py` | ❌ dead | **zero importers** |
| **S3B Requirement Patch** | `requirement_patch.py` | ❌ dead | **zero importers** |
| **S3B Timeout Manager** | `timeout_manager.py` | ⚠️ | imported by `event_wait_engine.py` only; reachable via resume/timeout |
| **S3C.C Event-Driven Wait** | `event_wait_engine.py` | ✅ | register/signal/await_and_resume all wired (agents endpoints + agent_service:2004-2016) |
| **S3C.D Adaptive Clarification** | `adaptive_clarification.py`, `clarification_budget.py` | ❌ 🧪 | imported ONLY by `test_sprint_3f1.py`; NOT wired to agent_service |
| **S3C.D ClarificationEngine** | `clarification_engine.py` | 🔌 | imported `agent_service.py:69` but **never called** |
| **S3C.E / 3F1 Progressive Context** | `progressive_context.py`, `context_memory.py`, `project_brain.py`, `repository_index.py`, `workspace_awareness.py`, `context_budget.py`, `knowledge_consistency.py`, `self_evaluation.py` | ❌ 🧪 | `progressive_context` imported ONLY by `test_sprint_3ce_integration.py`; the cluster is internally wired but the whole subtree is **NOT reachable from `main`** — agent_service still calls `ContextEngine.build_context` directly (1029), NOT `ProgressiveContextLoader`. **Migration incomplete.** |
| **S3C (worker execution path)** | `worker_service.py` + `main.py:170` | ✅ | worker loop started in lifespan |
| **S3C Job Queue** | `job_queue.py` | ✅ | `enqueue_job` in agents/jobs routers |
| **S3x Deploy** | `deploy_service.py` | 🔌 | imported `agent_service.py:52`, **never called** (dead import); `deployment_service.py` is the live one (`routers/deployments.py`) |
| **Sprint 3C (Forge-v2 plan endpoint)** | `agents.py:104` | ❌ broken | `ForgeV2Service` undefined |

> **Sprint 3C (as a distinct labeled sprint):** the brief lists "Sprint 3C" separately. Evidence shows
> sub-tracks 3C.C (event wait, ✅), 3C.D (adaptive clarification, ❌ orphan), 3C.E (progressive context,
> ❌ orphan). There is no single consolidated "3C" feature beyond these; treat 3C = the union above.

---

## PHASE 6 — DUPLICATE ARCHITECTURE DETECTION

| Duplicate class | Systems | Evidence | Live one |
|---|---|---|---|
| **Deploy services** | `DeployService` (`deploy_service.py`) vs `DeploymentService` (`deployment_service.py`) | `deploy_service` imported only by `agent_service.py:52` and **never called**; `deployment_service` used by `routers/deployments.py:*` | `DeploymentService` |
| **Websocket for jobs** | `/v1/ws/jobs/{id}` (`ws.py:57`, functional) vs `/agents/forge-v2/ws/{id}` (`agents.py:176`, `close(1008)`) | two WS routes for the same job-event purpose | `ws.py` |
| **Chat/plan routing** | `/forge-v2/run` pipeline (no chat branch) vs `/agents/route` `run_explicit_mode` (has `plan`→chat) | two entry surfaces, only the legacy one handles chat | fragmented (see Phase 4) |
| **Approval-helper functions (duplicated verbatim)** | `agent_service.py:80-122` vs `:128-170` | `_map_tool_to_approval_type`, `_assess_risk`, `_extract_file_paths`, `_extract_commands` defined **twice, identical**; first block (80-122) is shadowed/dead (Python keeps the second def) | second block (128-170) |
| **Context systems** | `ContextEngine.build_context` (live @1029) vs `ProgressiveContextLoader` (`progressive_context.py`, orphan) | two context orchestration layers; the "new" one is not wired | `ContextEngine` |
| **Clarification systems** | `ClarificationEngine` (imported, unused) vs `AdaptiveClarificationEngine` (test-only) | two clarification engines, neither invoked in production | none active |
| **Conversation systems** | `conversation_reliability.py` (live) vs `conversation_continuation.py` / `conversation_policy.py` (dead) | 3B engines superseded but not removed | `conversation_reliability` |
| **Forge generations** | forge-v1 (410 stubs) vs forge-v2 (live) | `agents.py:44-99` all 410 | forge-v2 |
| **`ApprovalSuspendSignal` vs `_ApprovalRequiredError`** | `models/agent.py:21` class vs `agent_service.py:78` alias | `_ApprovalRequiredError = ApprovalSuspendSignal` (alias, not a real duplicate) | single canonical (model-level) |

Not duplicates (verified single): routing (one classifier @1518), memory (`memory.py` single), requirement (single `requirement_discovery`; `requirement_patch` is dead not duplicate), orchestration (single `run_agent_pipeline`).

---

## PHASE 7 — ARCHITECTURE TIMELINE

**Legacy architecture (superseded, still present):**
- forge-v1 endpoints (`agents.py:44-99`) — now 410 GONE stubs.
- `ai_service.py` — pre-OAuth single-shot AI, **zero importers** (dead legacy).
- `agent_llm.py.bak`, `executor.py.orig` — backup files from interrupted edits (not in module graph).
- `test_endpoints.py` — top-level orphan script (`import requests`, not a dependency → unrunnable).
- 3B conversation engines (`conversation_continuation.py`, `conversation_policy.py`, `requirement_patch.py`) — built, never wired, now dead.
- Duplicate approval-helper block `agent_service.py:80-122` — leftover from a refactor, shadowed.

**Current architecture:**
- forge-v2 async queue: `/forge-v2/run` → `JobQueue` → `WorkerService.run` → `run_agent_pipeline` → code/server pipelines → executor.
- Event-driven wait/resume (3C.C) via `event_wait_engine.py`.
- Google-OAuth-only auth.
- Host-scoped gateway via `main.py:331` middleware (NOT a catch-all route) — regression `d016a14` fixed.
- Trailing-slash normalization middleware (`main.py:300`), `redirect_slashes=False`.

**Incomplete migrations:**
1. **Progressive Context (3C.E/3F1) never cut over.** The whole `progressive_context` cluster
   (8 modules) is internally consistent but the orchestrator still calls the *old*
   `ContextEngine.build_context` directly (`agent_service.py:1029`). The intended single call-site swap
   to `ProgressiveContextLoader().build(...)` was **not applied**. → orphan subtree reachable only from tests.
2. **Adaptive Clarification (3C.D) never wired.** `AdaptiveClarificationEngine` + `ClarificationBudget`
   exist and are tested but not invoked; the older `ClarificationEngine` is imported-but-unused.
3. **Chat routing** never migrated into the unified `run_agent_pipeline` (still only in legacy `/route`).

**Temporary compatibility layers:**
- forge-v1 410 stubs (explicit deprecation shims).
- `_ApprovalRequiredError = ApprovalSuspendSignal` alias (`agent_service.py:78`).
- `create_job` APIError fallback dropping `intent/errors/retries` columns (`agent_service.py:2206`) — schema-drift compat.

**Files still belonging to OLD architecture:**
`services/ai_service.py`, `services/conversation_continuation.py`, `services/conversation_policy.py`,
`services/requirement_patch.py`, `test_endpoints.py`, `services/agent_llm.py.bak`,
`services/executor.py.orig`, forge-v1 handlers in `routers/agents.py:44-99`, and the duplicate helper
block `agent_service.py:80-122`.

---

## PHASE 8 — FINAL REPORT

### 8.1 Architecture Diagram
```
                          ┌──────────────── FastAPI app (main.py:196) ─────────────────┐
Clients ── HTTPS ─▶ Middleware chain: log(238) → normalize_slash(300) → workspace_host→Gateway(331)
                          │
   ┌──────────────┬───────────────┬──────────────┬───────────────┬─────────────┐
 health  auth(Google)  servers/workspaces  chat  deployments   agents        jobs   ws(/v1/ws)
                                                     │                          │        │
                                            forge-v2/run(202)            POST /jobs   job_ws
                                                     ▼                          ▼      (live events)
                                            AgentService.submit_job → JobQueue.enqueue (DB)
                          ┌──────────────── lifespan background tasks (main.py) ───────┐
                          │  health loop   recovery loop   WORKER loop(170)            │
                          └───────────────────────────────┬───────────────────────────┘
                                                           ▼
                                   WorkerService.run → _claim_next_job → _execute_job
                                                           ▼
                                   AgentService.run_job → run_agent_pipeline
                     ┌─────────── discovery → classify_intent → task_mode ───────────┐
                     │  intent=code → _run_code_execution → ContextEngine + ImplIntel │
                     │  intent=server → build_plan → run_server_execution → executor  │
                     │  intent=chat → (NO BRANCH) → executor guard → INTENT_NOT_SERVER│  ❌
                     └───────── suspend → EventWaitEngine.register + await_and_resume ─┘
                                                           ▼
                                   Supabase (jobs, steps, events…) + Redis (pub/sub, cache, locks)
   Workspace subdomains ── Gateway proxy (host-scoped middleware, not a route)
```

### 8.2 Execution Diagram — see Phase 2 (verified node-by-node).

### 8.3 Pipeline Diagram — see Phase 3 table (13 live/partial, 8 dead/orphan).

### 8.4 Routing Diagram — see Phase 4 matrix. Two hard disconnects (`chat`, `forge-v2/plan`).

### 8.5 Sprint Integration Matrix — see Phase 5.
Summary: S1 ✅ · S2 ✅ · S3A ✅ · S3B ⚠️ (reliability core ✅, 3 engines dead) · S3C mixed
(3C.C ✅, 3C.D ❌ orphan, 3C.E/3F1 ❌ orphan — migration incomplete).

### 8.6 Legacy vs Current Matrix
| Concern | Legacy (present) | Current (live) |
|---|---|---|
| Agent API | forge-v1 (410) | forge-v2/run |
| AI exec | `ai_service.py` (dead) | `agent_llm.py` + `executor.py` |
| Context | `ContextEngine` (live) | `ProgressiveContext*` (orphan — NOT cut over) |
| Clarification | `ClarificationEngine` (unused) | `AdaptiveClarification*` (test-only) |
| Conversation | continuation/policy (dead) | `conversation_reliability.py` |
| Deploy | `DeployService` (dead import) | `DeploymentService` |
| Job WS | `/agents/.../ws` (close 1008) | `/v1/ws/jobs/{id}` |
| Auth | email/password (removed) | Google OAuth |
| Gateway | `/{path:path}` catch-all (removed) | host-scoped middleware |

### 8.7 Dead Code Report (proven — zero live importers or never called)
| Item | Evidence |
|---|---|
| `services/ai_service.py` | 0 importers |
| `services/conversation_continuation.py` | 0 importers |
| `services/conversation_policy.py` | 0 importers |
| `services/requirement_patch.py` | 0 importers |
| `test_endpoints.py` | top-level orphan, `import requests` unavailable |
| `services/agent_llm.py.bak`, `services/executor.py.orig` | backup files, not in graph |
| `agent_service.py:80-122` (4 helper fns) | verbatim redefined at 128-170 → shadowed |
| `from services.deploy_service import DeployService` (agent_service.py:52) | imported, never called |
| `from services.clarification_engine import ClarificationEngine` (agent_service.py:69) | imported, never called |
| `agents.py:176 forge_v2_ws` | unconditional `close(1008)` |
| forge-v1 handlers `agents.py:44-99` | 410 stubs |
| `progressive_context.py` + 7 sibling context modules | reachable only from tests (orphan cluster) |
| `adaptive_clarification.py`, `clarification_budget.py`, `knowledge_consistency.py`, `self_evaluation.py`, `workspace_awareness.py`, `repository_index.py`, `context_memory.py`, `project_brain.py`, `context_budget.py` | reachable only from tests |

> Note per project convention: `db/migrations/*.sql`, `db/schema.sql`, and empty `__init__.py` package
> markers are **NOT** dead code (infra/history/markers). Not listed above.

### 8.8 Duplicate System Report — see Phase 6 (9 duplications, most with a clear live winner).

### 8.9 Missing Connections Report
| # | Missing connection | Location | Impact |
|---|---|---|---|
| 1 | No `chat` branch in `run_agent_pipeline` | `agent_service.py:1577/1685` | chat jobs via `/forge-v2/run` → `INTENT_NOT_SERVER` fail |
| 2 | `ForgeV2Service` never imported/defined | `agents.py:113` | `/forge-v2/plan` 100% NameError |
| 3 | `ProgressiveContextLoader` not called at the context call-site | `agent_service.py:1029` (still `ContextEngine`) | 3C.E/3F1 features inert |
| 4 | `ClarificationEngine` / `AdaptiveClarificationEngine` not invoked | `agent_service.py:69` | clarification suspension never triggered from live path |
| 5 | 3B engines (continuation/policy/requirement_patch) not wired | `conversation_reliability.py` | 3B partial features inert |
| 6 | `DeployService` import unused | `agent_service.py:52` | dead dependency |
| 7 | forge-v2 WS dead; real WS on different prefix | `agents.py:176` vs `ws.py:57` | client confusion / no agent-namespace stream |

### 8.10 Architecture Health Score

**Overall: 6.0 / 10 — functional core, significant unfinished migration + two live routing breaks.**

| Dimension | Score | Rationale |
|---|---|---|
| Import-graph integrity | 9/10 | full graph imports clean in venv; breaks are runtime-only, not import-time |
| Primary path (forge-v2 code/server) | 8/10 | queue→worker→pipeline→executor solid, well-instrumented |
| Routing correctness | 4/10 | `chat` disconnect + `forge-v2/plan` NameError are user-facing failures |
| Sprint completion/wiring | 4/10 | 3C.D + 3C.E/3F1 entirely orphaned; 3B half-dead |
| Dead-code hygiene | 4/10 | ~10 dead modules/blocks + backups + duplicate helper block |
| Duplication | 5/10 | 9 duplicate systems, most with a clear winner but not removed |
| Reliability layer (approval/resume/wait) | 8/10 | 3A + 3C.C robust and connected |

**Top risks (evidence-backed):** (1) chat-intent hard-fail; (2) forge-v2/plan NameError; (3) large
orphaned context/clarification subtrees inflate surface area and mislead future work into thinking
3C.E/3F1 is active.

---

*End of read-only audit. No code was modified. Findings marked NOT VERIFIED: production scheduler
(none found), Telegram bot execution entry (none found), `show` intent (no emitter).*
