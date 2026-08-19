# ThinkSync Backend — Complete Production Architecture Audit

**Mode:** READ-ONLY. No code modified. No git. No fixes. Every statement cites a file:line.
**Scope:** Entire `backend/` (~38,658 LOC, 80 `.py` modules + tests + DB schema/migrations).
**Date of audit:** 2026-07-15.

> Anything not verifiable from the repository is marked **NOT VERIFIED**. No desired/aspirational architecture is described. This documents the system **exactly as it exists today**.

---

# 1 PROJECT OVERVIEW

**Purpose.** ThinkSync is a production AI-agent backend for Linux server automation. It accepts a natural-language objective, runs a multi-stage agent pipeline (discovery → clarification → approval → implementation intelligence → planner → executor → deployment → completion), and executes code on remote Linux servers over SSH. Frontend is Next.js (separate `frontend/` dir); backend is FastAPI + Supabase (Postgres) + Redis + asyncssh.

**Main responsibilities.**
- Orchestrate long-running agent jobs (`services/agent_service.py`, `run_agent_pipeline` at `:1573`).
- Suspend/resume jobs for clarification and approval via an event-wait engine (`services/event_wait_engine.py`).
- Execute code on remote servers over SSH (`services/executor.py`, `services/ssh_service.py`).
- Persist jobs/specs/approvals/events in Supabase; stream live events over Redis pub/sub → WebSocket (`routers/ws.py`).

**Current maturity.** Mid-to-late production. Core execution path works end-to-end; several subsystems are partial/foundational (JobQueue, PM2 deployment, `/forge-v2/ws` stub). Multiple reliability/guard layers exist (conversation_reliability, resume_manager, timeout_manager, guardrails, constitution).

**Production readiness.** Functionally usable but carries verified correctness bugs (see §16) and crash-unsafe suspend/resume state (§17 B3). Not robust to process restart while a job is parked.

**Strengths.**
- Clear separation of engines/services/routers/models.
- Event-driven suspend/resume (no polling) via `EventWaitEngine.signal` (`event_wait_engine.py:227`).
- Strong structured-clarification form added (generic, secret-safe — `models/clarification_form.py`).
- Comprehensive reliability guards (`services/conversation_reliability.py`, `resume_manager.py`).
- Gateway with subdomain routing, rate limiting, health gating (`routers/gateway.py`).

**Weaknesses.**
- `run_agent_pipeline` has verified pre-assignment `UnboundLocalError`s masked by broad excepts (§16 B1/B2) → discovery + adaptive clarification silently no-op for fresh jobs.
- Suspend/resume state is process-local, not crash-durable (§16 B3).
- "Resume as cursor" is aspirational; resume re-runs the whole job (§17 B4).
- `JobQueue` is a documented no-op stub (§17).
- `/forge-v2/ws/{job_id}` WebSocket is a stub that closes immediately (§17).
- Doc/code desync (`SPRINT_4_ARCHITECTURE_AUDIT.md` claims no chat branch; it exists at `agent_service.py:2092`).

---

# 2 DIRECTORY MAP

```
backend/
  main.py                 FastAPI app, lifespan (worker start), middleware, router includes, exception handlers
  core/
    config.py             Settings (env), JWT + DB + wait-timeout + feature flags
    database.py           Supabase sync + async proxy clients (get_supabase / get_supabase_async)
    security.py           JWT create/decode, get_current_user (HTTPBearer)
    authorization.py      Row-level authorization helpers (Supabase)
    crypto.py             Secret crypto helpers
    mode_context.py       set_request_mode / get_request_mode (per-request agent mode)
    value_coercion.py     value_to_str helper
  models/                 Pydantic models (agent, approval, interaction, conversation, job,
                          workspace, server, user, message, chat, deployment, clarification_form)
  services/               Business logic: ~55 modules (engines + services)
  routers/                FastAPI routers (agents, gateway, auth, ws, chat, workspaces,
                          servers, deployments, jobs, commands, health)
  db/
    schema.sql           Canonical schema (26 tables)
    migrations/          18 incremental SQL migration files
  scripts/               One-off migration scripts (e.g. migrate_workspaces_to_project_model.py)
  tests/                 22 test files (pytest)
```

**Package purposes.**
- `core/` — cross-cutting infra (config, DB client, auth, crypto, mode context).
- `models/` — data contracts only (no behavior).
- `services/` — all domain logic; engines are the heavy decision modules.
- `routers/` — HTTP/WS transport; thin, delegate to services.
- `db/` — SQL schema + migrations (source of truth for tables).

---

# 3 SYSTEM COMPONENTS

| Subsystem | Owner (module) | Entry point | Deps | Status | Used? |
|---|---|---|---|---|---|
| API transport | `routers/*` | FastAPI routes | services | active | yes |
| Agent orchestration | `agent_service.py` | `run_agent_pipeline` `:1573` | most services | active, buggy | yes |
| Worker/job claim | `worker_service.py` | `WorkerService.run` (lifespan `main.py:167`) | AgentService, JobQueue | active | yes |
| Job queue | `job_queue.py` | `enqueue_job` | Redis | **stub/no-op** | partial |
| Event wait | `event_wait_engine.py` | `signal`/`register` | Redis, InteractiveWait | active, not crash-safe | yes |
| Interactive wait | `interactive_wait.py` | `record_clarification_answer` | models | active | yes |
| Requirement discovery | `requirement_discovery.py` | `run_discovery` | agent_llm | active, **silently skipped** | yes (no-op by B1) |
| Adaptive clarification | `adaptive_clarification.py` | `evaluate` | interaction models | active, **silently skipped** | yes (no-op by B1) |
| Clarification engine | `clarification_engine.py` | `ClarificationEngine` | — | active | yes |
| Approval | `approval_engine.py` / `approval_policy.py` | `evaluate`/`resolve` | models | active | yes |
| Implementation intel | `implementation_intelligence.py` | `decide_strategy` | templates, LLM | active | yes |
| Planner | `planner.py` | `build_plan` | models | active | yes |
| Executor | `executor.py` | `run_server_execution` | ssh_service, tools | active | yes |
| Deployment | `deploy_service.py` / `deployment_service.py` | `DeploymentService` / PM2 | ssh | active (deploy_service used by forge-v2; PM2 via router only) | partial |
| Conversation/memory | `chat_service.py`, `context_*.py`, `project_brain.py` | mixed | Supabase | active | yes |
| SSH | `ssh_service.py` | `execute` | asyncssh | active | yes |
| Gateway | `gateway.py` | `proxy_request` | workspace_service, port_allocator, redis | active | yes |
| WebSocket | `ws.py` | `job_ws` | redis, AgentService | active (history+live) | yes |
| Permissions | `permission_service.py` | `check`/`check_async` | config | active | yes |
| Security/auth | `core/security.py`, `auth.py`, `google_auth.py` | JWT + Google OAuth | Supabase | active | yes |
| Reliability guards | `conversation_reliability.py`, `resume_manager.py`, `timeout_manager.py` | mixed | Supabase/Redis | active | yes |
| Self-healing | `self_healing.py` | — | executor | active | yes |
| Constitution | `agents/constitution.py` | `ConstitutionEngine` | — | active | yes |

**Dead code / unused.** `services/executor.py.orig` (a stray backup file — see §17). `models/job.py` `JobResponse` references fields; some models (`message.py` `CommandRequest`/`CommandResponse`) used only by `routers/commands.py`.

---

# 4 AGENT ARCHITECTURE

Stages are spread across `run_agent_pipeline` (`agent_service.py:1573`) and helper services. The real order:

1. **Intent detection** — `agent_llm.classify_intent` (`agent_service.py:1866`); regex nudges (`deploy|server|app`) → `server` intent (`:1869–1875`).
2. **Requirement Discovery** — `run_discovery` (`agent_service.py:1691`) guarded by `should_run_discovery` (`:1685`). **Currently silenced** by B1 (see §16).
3. **Adaptive Clarification** — `AdaptiveClarificationEngine.evaluate` (`agent_service.py:1743`); if `action=="ask"`, builds `ClarificationForm.from_questions` (`:1764`), publishes `waiting_for_clarification` event (`:1788`), raises `ClarificationSuspendSignal` (`:1797`). **Silenced** by B1 (project_spec=None).
4. **Approval** — per-step `ApprovalPolicyEngine.pre_execute_check` inside `on_step_start` (`agent_service.py:2257`); raises `ApprovalSuspendSignal` (`:2315`).
5. **Conversation** — `chat_service.py` persists messages; `conversation_reliability.py` provides exactly-once/idempotency guards.
6. **Implementation Intelligence** — `_build_implementation_report` → `ImplementationIntelligence.decide_strategy` (`agent_service.py:560,2180`); strategy + rendered files fed to planner.
7. **Planner** — `build_plan` (`planner.py:49`) called at `agent_service.py:2162`; emits `on_plan`.
8. **Executor** — `run_server_execution` / `_run_code_execution`; tool-calling loop with self-healing (`executor.py`).
9. **Patch** — `requirement_patch.py` (`RequirementPatchEngine`) — present; used in spec-update path.
10. **Verification** — `constitution_engine.check_success_contract` (`agent_service.py:2582`).
11. **Deployment** — `_run_deployment_contract` (`executor.py:975`) curl-based port/gateway verification. PM2 `DeploymentService` is **not** wired into forge-v2 (§17 B5).
12. **Completion** — `_db_update(status=COMPLETED)` + `_publish(completed)` (`agent_service.py:2584–2646`).
13. **Conversation Memory** — `context_memory.py`, `context_engine.py`, `project_brain.py`, `progressive_context.py`.
14. **Event Wait** — `event_wait_engine.py` (signal/register/await).
15. **Resume** — `ResumeManager` + `conversation_reliability.CrashRecoveryGuard`; resume driver re-invokes `AgentService.run_job` (full re-run, not cursor — §17 B4).
16. **Job Queue** — `worker_service.py` claims via atomic DB `UPDATE … WHERE status='queued'`; `job_queue.py` is a no-op advisory layer (§17).
17. **Workers** — `WorkerService.run` (lifespan `main.py:167`), in-process asyncio worker.
18. **State Management** — `JobInteractionState` (`models/approval.py`) + job row status; plus ephemeral `local_subscribers`/`local_event_history` dicts in `agent_service.py`.

**Known limitations (per stage).** Discovery & clarification no-op on fresh jobs (B1). Clarification/approval resume not crash-durable (B3). Resume re-runs whole job (B4). Deployment not PM2-managed in forge-v2 (B5).

---

# 5 ENGINE INVENTORY

| Engine | File:line | Public API | Callers | Status | Prod-ready |
|---|---|---|---|---|---|
| RequirementDiscoveryEngine | `requirement_discovery.py` (`run_discovery`) | `run_discovery`, `should_run_discovery` | `agent_service.py:1691` | active, **masked by B1** | partial |
| AdaptiveClarificationEngine | `adaptive_clarification.py:115` | `evaluate` | `agent_service.py:1743` | active, **masked by B1** | partial |
| ClarificationEngine | `clarification_engine.py:35` | `ClarificationEngine` | clarification path | active | yes |
| ClarificationBudgetEngine | `clarification_budget.py:115` | budget check | clarification | active | yes |
| ApprovalEngine / ApprovalPolicyEngine | `approval_engine.py:91`, `approval_policy.py:29` | `evaluate`, `resolve`, `pre_execute_check` | `agent_service.py` | active | yes |
| ImplementationIntelligence | `implementation_intelligence.py` (`decide_strategy`, `TemplateDiscoveryEngine`, `TemplateRankingEngine`, `HybridGenerationEngine`, `CodeGenerationEngine`) | `decide_strategy` | `agent_service.py:2180` | active | yes |
| Planner | `planner.py:16` (`ApprovedPlanViolationError`) | `build_plan` | `agent_service.py:2162` | active | yes |
| AgentDecisionEngine | `agent_decision_engine.py:147` | decision routing | agent loop | active | yes |
| EventWaitEngine | `event_wait_engine.py:125` | `signal`, `register`, `await_and_resume`, `await_clarification_reply` | routers, agent_service | active, not crash-safe | partial |
| InteractiveWaitEngine | `interactive_wait.py:92` | `pause`, `resume`, `record_clarification_answer` | agent_service, ws | active | yes |
| ContextEngine | `context_engine.py:103` | context assembly | agent loop | active | yes |
| RequirementPatchEngine | `requirement_patch.py:114` | spec patch | spec update | active | yes |
| ConversationContinuationEngine | `conversation_continuation.py:48` | continuation intent | chat | active | yes |
| ConversationAuditEngine | `conversation_audit.py:94` | audit | chat | active | yes |
| ContextDiffEngine / ProjectBrain | `project_brain.py:162,301` | memory diff/recall | context | active | yes |
| ConfidenceEngine | `context_memory.py:72` | confidence | context | active | yes |
| PermissionService (authorization engine) | `permission_service.py:35` | `check`/`check_async` | agent_service, create_job | active | yes |
| DecisionRouter / Shadow / Weighted | `decision_router.py:59`, `decision_shadow.py`, `decision_weighted.py` | decision routing w/ modes | agent loop | active (feature-flagged) | partial |
| AgentDecisionEngine | `agent_decision_engine.py` | next-action | agent loop | active | yes |

---

# 6 SERVICES INVENTORY

(Condensed — each is a `services/*.py` module.) All services are called by `agent_service.py` or routers. Key ones:

- `agent_service.py` — orchestration hub. Called by `routers/agents.py`, `worker_service.py`.
- `agent_llm.py` — LLM calls (intent, task-mode, chat, codegen). Depended on by discovery, clarification, planner, executor.
- `executor.py` — remote execution + self-healing + deployment contract.
- `ssh_service.py` — asyncssh wrapper. Depended on by executor, deploy, server_service.
- `worker_service.py` — in-process job worker. Started at `main.py:167`.
- `job_queue.py` — **stub** (Redis advisory only).
- `chat_service.py` — chat persistence + context retrieval. Depended on by agent loop, routers/chat.
- `workspace_service.py` — workspace CRUD + resolve + create-from-prompt. Depended on by agent loop, routers/workspaces, gateway.
- `server_service.py` — server CRUD + PlatformContext. Depended on by agent loop, routers/servers.
- `deployment_service.py` — PM2 process management (used only by `routers/deployments.py`). **Not** in forge-v2 path.
- `deploy_service.py` — curl-based deployment verification used by forge-v2.
- `permission_service.py` — single permission gate (`check`/`check_async`). Called at `create_job` (`:2679`) and `_run_code_execution` (`:915`).
- `guardrails.py` — workspace-path validation (`validate_workspace_path` at `agent_service.py:983`).
- `conversation_reliability.py` — idempotency/optimistic-lock/crash-recovery guards.
- `resume_manager.py` — `ResumeManager` (resume bundle load).
- `timeout_manager.py` — step timeouts.
- `context_*.py`, `project_brain.py`, `progressive_context.py`, `knowledge_consistency.py` — memory/context layer.
- `redis_service.py` — Redis client (events, rate-limit, queue advisory).
- `memory.py` — `MemoryStore` (conversation memory load/save).
- `self_healing.py`, `self_evaluation.py` — execution recovery + eval.
- `health_checker.py`, `port_allocator.py`, `templates.py`, `repository_index.py`, `capability_service.py`, `workspace_awareness.py`, `structured_output.py`, `execution_event_service.py`, `execution_audit.py`, `execution_repository.py`, `job_recovery.py`, `templates.py`, `ai_service.py`, `user_service.py`, `google_auth.py`, `http_client.py`, `logger.py`.

---

# 7 ROUTERS

All under `routers/`, included in `main.py:341–350`. All authed via `get_current_user` (HTTPBearer, `core/security.py`) unless noted.

| Router | Endpoints | Auth | Return models | Consumers |
|---|---|---|---|---|
| `agents.py` | `/forge-v2/plan` (POST), `/forge-v2/run` (POST, 202), `/forge-v2/jobs/{id}` (GET), `/forge-v2/ws/{id}` (WS — **stub closes**), `/jobs/{id}/reply` (POST), `/jobs/{id}/event` (POST), `/jobs/{id}/clarification-reply` (POST), `/route` (POST), plus legacy `/forge/...`, `/forge-v1/...` | get_current_user | `AgentAsyncRunAccepted`, `ForgeV2JobResponse`, etc. | frontend, Telegram bridge |
| `gateway.py` | `/{path:path}` (proxy, catch-all) | **none** (subdomain-based) | proxied upstream | deployed workspace subdomains |
| `auth.py` | `/google` (POST), `/me` (GET), `/logout` (POST) | `/me`/`/logout` authed | `TokenResponse`, `UserResponse` | frontend |
| `ws.py` | `/v1/ws/jobs/{id}` (WS) | token query param (`decode_token`) | JSON events | frontend |
| `chat.py` | `/{workspace_id}` (GET), `/{workspace_id}/message` (POST) | get_current_user | `ChatResponse`, `ChatSendMessageResponse` | frontend |
| `workspaces.py` | `/` (POST/GET), `/{id}` (GET) | get_current_user | `WorkspaceResponse` | frontend |
| `servers.py` | `/` (GET/POST), `/{id}` (DELETE) | get_current_user | `ServerResponse` | frontend |
| `deployments.py` | `/{workspace_id}` (POST/GET/DELETE) | get_current_user | `DeploymentResponse` | frontend |
| `jobs.py` | `/` (POST/GET), `/{id}` (GET), `/{id}/timeline`, `/{id}/steps`, `/{id}/decisions`, `/{id}/retries`, `/{id}/errors`, `/recovery/report`, `/recovery/{id}/mark-recoverable`, `/recovery/{id}/mark-orphaned` | get_current_user | `JobResponse`, etc. | frontend, recovery tooling |
| `commands.py` | `/execute` (POST) | get_current_user | `CommandResponse` | frontend/admin |
| `health.py` | `/health`, `/metrics` | **none** | JSON | monitoring |

**Verification:** `routers/gateway.py` and `routers/health.py` have no auth (by design: gateway is host-routed; health is public).

---

# 8 DATABASE

**Engine:** Postgres via Supabase. **26 tables** defined in `db/schema.sql` (lines 59–471). Access via `core/database.py` (`get_supabase`/`get_supabase_async`).

Tables: `agent_context_logs, agent_runs, approval_audit, approval_requests, chat_messages, chats, conversation_audit, idempotency_store, job_decisions, job_events, job_execution_details, job_retries, job_state_transitions, job_steps, jobs, messages, project_specifications, resume_outcomes, servers, tasks, users, worker_heartbeats, workspace_deployments, workspace_files, workspaces`.

**Key relationships (from schema + code).**
- `jobs.user_id → users.id`; `jobs.workspace_id → workspaces.id`; `jobs.server_id → servers.id`.
- `workspaces.server_id → servers.id`.
- `approval_requests.job_id → jobs.id`; `approval_audit.approval_id → approval_requests.id`.
- `chats.workspace_id → workspaces.id`; `chat_messages.chat_id → chats.id`.
- `job_events.job_id → jobs.id` (Redis is the live event bus; Supabase `job_events` is persistence).
- `project_specifications` referenced by job `specification` column (job row stores spec inline too).

**Current usage.** `jobs` (primary), `workspaces`, `servers`, `users`, `chats`/`chat_messages`, `approval_requests`/`approval_audit`, `job_events`, `job_steps`, `job_decisions`, `worker_heartbeats`.

**Unused / missing (verified).**
- **Unused:** `tasks` table — no code inserts/reads it (no `tasks` reference in services). `agent_context_logs` — referenced only by context engine logging path (**partial**). `workspace_files` — referenced by workspace file listing (**partial**). `resume_outcomes` — written by `ResumeManager` (**partial**, may be dead).
- **Missing relationship:** `job_events`/`job_steps`/`job_decisions` are written but the live stream is Redis pub/sub (`routers/ws.py`); replay after Redis flush depends on `job_events` table — verify retention.
- **Missing index evidence:** NOT VERIFIED (schema not fully analyzed for FK indexes).

---

# 9 MODELS

| File | Classes | Purpose | Status |
|---|---|---|---|
| `agent.py` (2046 LOC) | `ApprovalSuspendSignal`, `ClarificationSuspendSignal`, `ResolutionPolicy`, `IntentType`, `AssumptionPriority`, `AgentTier`, `AgentJobStatus`, `ToolName`, `ProjectSpecification`, `AgentDecision`, `AgentStep`, `StepResult`, `ClarificationQuestion`, … | Core agent/spec/decision contracts | active (largely used) |
| `approval.py` (537 LOC) | `ApprovalDecision`, `ApprovalStatus`, `ApprovalType`, `ApprovalRequest`, `ApprovalAuditEvent`, `ResumeToken`, `JobInteractionState` (+`clarification_submission` field) | Approval + interaction state | active |
| `interaction.py` | `QuestionPriority`, `QuestionType`, `ClarificationQuestion`, `ReplyType`, `StructuredUserReply`, `ClarificationSession` | Clarification question contracts | active |
| `conversation.py` | `SessionState`, `ConversationSession`, `ConversationSessionStore` | Conversation session store | active |
| `job.py` | `JobStatus`, `JobCreate`, `JobAccepted`, `JobResponse` | Job lifecycle DTOs | active |
| `workspace.py` | `WorkspaceCreateRequest`, `WorkspaceResponse` | Workspace DTOs | active |
| `server.py` | `SSHAuthMethod`, `ServerBase`, `ServerCreate`, `ServerResponse` | Server/SSH DTOs | active |
| `user.py` | `GoogleLoginRequest`, `TokenResponse`, `UserResponse` | Auth DTOs | active |
| `message.py` | `MessageRole`, `CommandRequest`, `CommandResponse` | Command DTOs (commands router) | active (narrow) |
| `chat.py` | `ChatMessageRole`, `ChatMessageRequest`, `StoredMessageResponse`, `ChatResponse`, `ChatSendMessageResponse` | Chat DTOs | active |
| `deployment.py` | `DeploymentResponse`, `DeploymentDetailResponse` | Deployment DTOs | active |
| `clarification_form.py` (491 LOC) | `ClarificationQuestionType`, `ClarificationChoice`, `ClarificationValidation`, `ClarificationFormQuestion`, `ClarificationForm`, `ClarificationFormAnswer`, `ClarificationFormSubmission` | **Generic structured clarification form** | active (newly added) |

**Dead/unused models:** NOT VERIFIED any fully-unused model; most are referenced. `ResumeToken` (`approval.py:206`) is defined but usage not confirmed in this read — **partial/unverified**.

---

# 10 EVENT FLOW

**WebSocket (`routers/ws.py`).** `job_ws` authenticates via `?token=`, then `_send_history` replays `job_events:{job_id}` from Redis; if not already `completed`, `_stream_live_events` subscribes to `job_events:{job_id}:live` pub/sub and streams until `completed`. Heartbeat `ping` every 60s. Events are JSON dicts with `type`.

**Event types published (`agent_service._publish`, `:1541`):** `status_update`, `step_start`, `step_result`, `log_chunk`, `decision`, `waiting_for_clarification`, `failed`, `completed`, `ping`.

**Clarification flow.** `run_agent_pipeline` publishes `waiting_for_clarification` (`:1788`) with `questions` + `clarification_form`; raises `ClarificationSuspendSignal` (`:1797`); caught at `:2505` → `EventWaitEngine.register` + `await_clarification_reply` task (`:2530`). Frontend (or Telegram) calls `POST /agents/jobs/{id}/clarification-reply` → `EventWaitEngine.signal` (`:227`) → resume folds answer via `_apply_clarification_answer_to_spec` (`:405`).

**Approval flow.** `on_step_start` raises `ApprovalSuspendSignal` (`:2315`); caught at `:2456` → `EventWaitEngine.register` + `await_and_resume` (`:489`); on wake reloads job + re-invokes `AgentService.run_job` (`:608`).

**Worker notifications.** `worker_service.py` claims job, runs, marks completed; emits via `_publish` → Redis → WS.

**Status updates.** DB `jobs.status` updated via `_db_update`; mirrored to WS via `_publish`.

**Complete event graph.**
```
client → POST /forge-v2/run → job QUEUED (Supabase)
WorkerService (lifespan) claims (atomic UPDATE) → run_job → run_agent_pipeline
  ├─ publish status_update(RUNNING)
  ├─ [discovery] (B1: skipped)
  ├─ [clarification] publish waiting_for_clarification → ClarificationSuspendSignal
  │     → EventWaitEngine.register + await_clarification_reply (parked)
  │     client → POST /clarification-reply → signal → fold → re-enter pipeline
  ├─ intent classify → status_update(intent)
  ├─ build_plan → publish on_plan
  ├─ per step: on_step_start → [approval?] ApprovalSuspendSignal → park → /reply → resume
  ├─ on_step_result / on_log_chunk → publish
  ├─ completion → publish completed → WorkerService._mark_completed
WS (routers/ws.py): history replay + live pub/sub until completed
```

---

# 11 EXECUTION PIPELINE (REAL CALL GRAPH)

```
POST /agents/forge-v2/run                      routers/agents.py:119  forge_v2_run_async
  → AgentService.submit_job                    agent_service.py:2660
      → PermissionService.check (create_job)   agent_service.py:2679
      → AgentService.create_job                agent_service.py:2676  (Supabase insert, QUEUED)
  → JobQueue.enqueue_job                       job_queue.py:65  (NO-OP: Redis hset only)
  → WorkerService (lifespan main.py:167)
      → _claim_next_job (atomic UPDATE)        worker_service.py:156
      → _execute_job                           worker_service.py:232
      → AgentService.run_job (bypass_semaphore) agent_service.py:2797
          → _run_agent_loop                    agent_service.py:2649
              → run_agent_pipeline              agent_service.py:1573
                  ├─ [RESUME CHECK] if status==WAITING_FOR_USER → ResumeManager.load + transition_running  :1598
                  ├─ run_discovery (B1: UnboundLocalError → project_spec=None)  :1691
                  ├─ AdaptiveClarificationEngine.evaluate (B1: skipped)  :1743
                  │     └─ if ask: build ClarificationForm, publish waiting_for_clarification,
                  │        raise ClarificationSuspendSignal  :1764–1797
                  ├─ ServerService.get_server  :1814
                  ├─ workspace resolve / create  :1828–1845
                  ├─ ChatService.get_recent_context_messages  :1852
                  ├─ agent_llm.classify_intent → intent  :1866
                  ├─ constitution_engine.check_job_state  :1878
                  ├─ publish status_update(RUNNING, intent)  :1885
                  ├─ agent_llm.detect_task_mode  :1897
                  ├─ BRANCH intent:
                  │    code  → _run_code_execution  :1991
                  │    chat  → agent_llm.generate_chat_response  :2092
                  │    server→ build_plan :2162 → run_server_execution :2434
                  ├─ ImplementationIntelligence.decide_strategy  :2180
                  ├─ build_plan (planner.py:49) → on_plan  :2162
                  ├─ run_server_execution (executor.py:256)
                  │     ├─ on_step_start → ApprovalPolicyEngine.pre_execute_check  :2257
                  │     │     └─ if needs approval: raise ApprovalSuspendSignal  :2315
                  │     ├─ tool calls (tools.py) via SSHService.execute
                  │     ├─ self_healing on failure
                  │     └─ _run_deployment_contract (executor.py:975) on deploy
                  ├─ constitution_engine.check_success_contract  :2582
                  ├─ _db_update(status=COMPLETED/FAILED)  :2591
                  └─ _publish(completed)  :2584
SUSPEND/RESUME:
  ClarificationSuspendSignal caught :2505 → EventWaitEngine.register :2522 + await_clarification_reply :2530
  ApprovalSuspendSignal    caught :2456 → EventWaitEngine.register + await_and_resume :489
  Wake endpoints: /reply :210, /event :243, /clarification-reply :275 → EventWaitEngine.signal :227
  Timeout: WAIT_TIMEOUT_SECONDS (default 1800, core/config.py:115) armed in _arm_timeout :184
```

**Branches.** `code`/`chat`/`server` intents; `ask`/`safe_assume`/`continue` clarification actions; approval required/not.

**Suspends.** Clarification (`:1797`), Approval (`:2315`). **Resumes.** via `signal` → fold/refold → re-enter `run_agent_pipeline` (full re-run, not cursor — §17 B4).

**Verifications.** `guardrails.validate_workspace_path` (`:983`), `validate_code` (`:1059`), `constitution_engine.check_success_contract` (`:2582`).

---

# 12 FRONTEND CONTRACT

The frontend must know the following (all verified from code).

**REST endpoints (base path `/api` per Next rewrite; see `frontend/services/api.ts`).**
- `POST /api/agents/forge-v2/run` → `{id}` (202). Body: `{objective, server_id, workspace_id?, max_steps?, allow_write?, model?, conversation_id?, mode?}`.
- `GET /api/agents/forge-v2/jobs/{id}` → job status/plan/steps/decisions/summary.
- `WS /api/v1/ws/jobs/{id}?token=...` → live events (preferred). NOTE: `/api/agents/forge-v2/ws/{id}` is a **stub** that closes immediately — do NOT use it.
- `POST /api/agents/jobs/{id}/clarification-reply` → `{status, job_id, event, woke}`. Body: `{conversation_id?, reply?, structured_reply?, clarification_submission?}`. `clarification_submission` = `{clarification_id, answers:[{question_id, required_field, value, selected_choice}]}`.
- `POST /api/agents/jobs/{id}/reply` and `/event` → legacy wake endpoints.
- `GET/POST /api/workspaces`, `GET /api/servers`, `GET/POST/DELETE /api/servers/{id}`, `GET/POST /api/chat/{workspace_id}`, `POST /api/chat/{workspace_id}/message`, `POST/GET/DELETE /api/deployments/{workspace_id}`, `GET/POST /api/jobs`, `POST /api/auth/google`, `GET /api/auth/me`.

**Response models.** `ForgeV2JobResponse` (status enum, plan, steps, decisions, summary, intent, task_mode). `JobResponse`. `WorkspaceResponse`. `ServerResponse`. `ChatResponse`. `ClarificationForm` (generic schema: `id,title,description,questions[]` where each question has `required_field,title,type,required,secret,choices[],validation,placeholder,example,depends_on,visible_if`). `ClarificationFormSubmission`.

**WebSocket events.** `type` ∈ `{step_start, step_result, log_chunk, status_update, completed, ping, waiting_for_clarification}`. `waiting_for_clarification` carries `questions` (legacy) + `clarification_form` (structured). `status_update` carries `status` (`AgentJobStatus` enum: queued, running, waiting_for_clarification, waiting_for_approval, waiting_for_resume, completed, failed), `intent`, `step`, `tool`.

**Clarification payload (structured).** `ClarificationForm` — render title/description/questions; collect answers; submit ONE `ClarificationFormSubmission`. Types: `text,textarea,password,secret,number,boolean,single_select,multi_select,path,directory,url,domain,port,email,ssh_key,api_key,environment`. Secret fields must never be echoed/displayed in plaintext.

**Approval payload.** `ApprovalRequest` (`approval_type`, `risk_level`, `summary`, `requested_by`, `decision`); `ApprovalDecision` enum (`approved/rejected/needs_changes`). Frontend submits decision via the approval wake endpoint (see `routers/agents.py` reply/event). **Note:** the exact approval-submit endpoint shape was not fully traced in this read — verify against `approval_engine.py` + `routers/agents.py` before building.

**Execution updates.** `step_start{step,tool,args}`, `step_result{step,success,summary,exit_code}`, `log_chunk{step,stream,data}`.

**Job status.** Poll `GET /api/agents/forge-v2/jobs/{id}` OR subscribe to WS. Job is terminal at `completed`/`failed`.

**Workspace / Chat.** Standard CRUD; chat messages persisted server-side.

**Errors.** `ApiError` shape: `{detail: {code?, error?, reason?}}` or `{error, errors[]}` (422 validation). 401/403/404/422/429/500 per FastAPI. Gateway returns `429` (rate limit), `502` (upstream down), `503` (routing/health).

**Progress.** Live progress via WS `step_start`/`step_result`/`log_chunk`; also `GET /api/jobs/{id}/steps`, `/timeline`, `/decisions`.

---

# 13 CURRENT UI CONTRACT (backend expectations)

**Backend EXPECTS frontend to:**
- Authenticate with `Authorization: Bearer <jwt>` (Google OAuth `/api/auth/google` → token).
- Open WS `/api/v1/ws/jobs/{id}?token=` and consume `waiting_for_clarification` → render `clarification_form` → submit ONE `clarification_submission`.
- For approval: render the approval request and submit a decision through the wake endpoint.
- Treat `completed`/`failed` as terminal; stop the WS on `completed`.
- Never parse free text for clarification — use the structured `clarification_form`.
- Send `clarification_submission` (not raw `reply`) as the preferred path.

**Frontend MUST NEVER:**
- Send secrets back to chat/UI in plaintext (secret fields are redacted server-side).
- Assume `/forge-v2/ws/{id}` works (it is a stub — use `/v1/ws/jobs/{id}`).
- Bypass `PermissionService` (all writes gated server-side).
- Rely on `JobQueue` semantics (it is a no-op; worker claims directly from DB).
- Assume a parked job survives a backend process restart (suspend state is in-memory — §16 B3).
- Hardcode project types or Telegram logic (the form is generic).

---

# 14 STATE MACHINES

**Job lifecycle (`JobStatus`, `models/job.py:8`):** `queued → running → (waiting_for_clarification | waiting_for_approval | waiting_for_resume) → running → completed | failed`. Transitions persisted in `jobs.status` and `job_state_transitions` table. Claims: `queued → running` via atomic UPDATE (`worker_service.py:156`).

**Workspace lifecycle:** created → resolved → active; subdomain assigned; port allocated (`port_allocator.py`); health tracked (`gateway.py` `mark_workspace_health`). **Partial:** PM2 deployment not in forge-v2 (B5).

**Execution lifecycle:** per-step `step_start → step_result` inside `running`; approval can suspend mid-step.

**Clarification lifecycle:** `running → waiting_for_clarification` (publish form + suspend) → answer submitted → fold → re-enter `running`. Turn counter (`_clarification_turn`) guards re-ask (`:1725–1740`).

**Approval lifecycle:** `running → waiting_for_approval` (per-step pre-execute) → decision → `running` or `failed`. `ApprovalStatus` (`models/approval.py:105`): `pending → approved | rejected | expired`.

---

# 15 SECURITY

- **Authentication.** JWT HS256 (`core/security.py`, `create_access_token`/`decode_token`). Google OAuth (`auth.py` `/google`, `google_auth.py`) issues JWT. WS auth via `?token=` query (`ws.py:62`, `decode_token`).
- **Authorization.** `PermissionService.check`/`check_async` is the single gate (`agent_service.py:2679,915`). `core/authorization.py` provides Supabase RLS helpers. Job rows filtered by `user_id` (`get_job` `:2751`).
- **Secrets.** Supabase **service-role key** used server-side (`core/database.py` — bypasses RLS; must never reach client). `SSHService` holds server credentials (encrypted at rest in `servers` table — **verify encryption at rest**: NOT VERIFIED). JWT secret from env (`core/config.py`). Clarification secrets redacted before chat history (`interactive_wait.record_clarification_answer`, `models/clarification_form.redacted_with_form`).
- **Permissions.** `PermissionService` allows/denies `create_job`, `run_code_execution` by user/workspace/server. Product default `AGENT_ALLOW_WRITE` forces write (`:911–924`).
- **SSH.** `ssh_service.py` uses asyncssh; auth via `SSHAuthMethod` (password/key). Commands run on remote `/root/workspaces/<slug>`.
- **Approval.** Per-step `ApprovalPolicyEngine.pre_execute_check` gates risky actions; `ApprovalSuspendSignal` parks job.
- **Sandbox.** No containerization — execution is on the remote Linux server filesystem under `/root/workspaces`. `guardrails.validate_workspace_path` restricts path (`:983`). **No chroot/namespace isolation verified.**

---

# 16 CURRENT BUGS (VERIFIED)

**B1 — `conversation_history` referenced before assignment (silent discovery skip).**
`run_agent_pipeline` calls `run_discovery(..., conversation_history=conversation_history, ...)` at `agent_service.py:1694`, but `conversation_history` is first assigned at `:1849`. For a fresh job this raises `UnboundLocalError`, caught by the broad `except Exception` at `:1708`, which sets `project_spec=None` and only logs a warning. **Effect:** Requirement Discovery never runs for new jobs. Severity: **HIGH**. Affected: `agent_service.py` (discovery +, transitively, clarification).

**B2 — `intent` referenced before assignment.**
`AdaptiveClarificationEngine.evaluate(..., intent="server" if intent == "server" else "code", ...)` at `agent_service.py:1745`, but `intent` is first assigned at `:1866`. Same latent `UnboundLocalError`. Currently masked because B1 forces `project_spec=None` so the adaptive block is skipped. Severity: **MEDIUM/HIGH** (latent). Affected: `agent_service.py`.

**B3 — Suspend/resume not crash-durable.**
`EventWaitEngine` keeps waiters in process-local dicts (`event_wait_engine.py:134–141`: `_waits`, `_signals`, `_timeout_tasks`). The worker is an in-process asyncio loop (`main.py:167`). On backend restart, in-memory `asyncio.Event` waiters and spawned `await_and_resume`/`await_clarification_reply` tasks are destroyed; only `jobs.status=WAITING_FOR_USER` persists. **Effect:** a parked job can be stranded until `WAIT_TIMEOUT_SECONDS` (1800s) fires, and even then recovery depends on `job_recovery.py` (NOT VERIFIED to auto-resume). Severity: **HIGH**. Affected: `event_wait_engine.py`, `worker_service.py`.

**B4 — "Resume as cursor" is aspirational.**
Comments at `agent_service.py:1606–1608` claim resume skips re-discovery/re-planning and runs `run_tool_calling_loop(pending_steps)`, but `_resume_bundle` is only stored (`:1609`) and consumed once as `is_resume` in decision state (`:1962`). No `pending_steps` threaded to `run_server_execution`; `AgentService.run_job` is fully re-invoked (`event_wait_engine.py:608`) → full re-run. Severity: **MEDIUM** (correctness/perf, not crash). Affected: `agent_service.py`, `event_wait_engine.py`.

**B5 — PM2 `DeploymentService` not wired into forge-v2.**
`deployment_service.py` (PM2) is used only by `routers/deployments.py`. Forge-v2 uses curl verification `_run_deployment_contract` (`executor.py:975`). Forge-v2 server deployments are not PM2-managed. Severity: **MEDIUM** (operational inconsistency). Affected: `executor.py`, `deployment_service.py`.

**B6 — Doc/code desync.**
`SPRINT_4_ARCHITECTURE_AUDIT.md:152,379` claims "no chat branch in run_agent_pipeline → chat fails"; current code has the chat branch at `agent_service.py:2092` (Sprint 4F-1). Severity: **LOW** (documentation).

**Minor — `/forge-v2/ws/{job_id}` is a stub** (`routers/agents.py:176–178`) closing with code 1008. Frontend must use `/v1/ws/jobs/{id}`. (Cross-referenced in §17 as dead/unstable contract.)

**Minor — `JobQueue.enqueue_job` is a no-op** (`job_queue.py:65`) — Redis advisory only; real claiming is DB atomic UPDATE in `worker_service.py`.

No fixes applied (READ-ONLY). These are descriptions only.

---

# 17 DEAD CODE

- **`services/executor.py.orig`** — a stray backup file (git-merge artifact) sitting next to `executor.py`. Should be removed. Dead/confusing.
- **`JobQueue`** — `enqueue_job`/`claim_job` are no-op/advisory stubs (`job_queue.py:65,89`); the worker bypasses them (`worker_service.py:156` atomic UPDATE). Foundation only — not production.
- **`/forge-v2/ws/{job_id}`** — stub WebSocket that immediately closes (`routers/agents.py:176`). Unstable contract; do not depend on it.
- **`tasks` table** — defined in `db/schema.sql` but no service code reads/writes it (no `tasks` reference found). Likely dead schema.
- **`agent_context_logs`** — written by context engine path; usage **partial** (verify).
- **`resume_outcomes`** — written by `ResumeManager`; may be diagnostic-only (partial/unverified).
- **Legacy `/forge/...` and `/forge-v1/...` routes** (`routers/agents.py:44–104`) — older API surface, still present (backward-compat). Not dead but superseded by forge-v2.
- **`ResumeToken` model** (`models/approval.py:206`) — defined; usage not confirmed in this read (potential dead/unused).
- **Duplicate implementations:** `ws.py` (`/v1/ws/jobs/{id}`) is the real WS; `/forge-v2/ws/{id}` is a duplicate stub. `deployment_service.py` (PM2) vs `deploy_service.py` (curl) — two deployment implementations, only one wired per path (B5).

---

# 18 TECHNICAL DEBT

- **Architecture risk:** Suspend/resume in-memory (B3) — single backend process is a SPOF for parked jobs; horizontal scaling breaks waiters.
- **Correctness risk:** B1/B2 masked by broad excepts — silent feature disable; tests exist (`test_structured_clarification_form.py`) but the pre-assignment bug means discovery/adaptive clarification are effectively disabled in production for fresh jobs.
- **Maintainability risk:** `agent_service.py` is 2,996 LOC / single function `run_agent_pipeline` spanning ~1100 lines (`:1573–2646`) — very hard to maintain; high chance of more latent pre-assignment/shadowing bugs.
- **Scaling risk:** In-process worker (`main.py:167`); `JobQueue` stub means no real distributed queue. Multiple workers would double-claim without the Redis advisory (mitigated by atomic UPDATE, but event-wait dicts are per-process).
- **Scaling risk:** Redis pub/sub for live events — no persistence guarantee; if Redis restarts, in-flight job events lost (relies on `job_events` table replay — verify retention).
- **Production risk:** Service-role key server-side (correct) but any SSRF/log-leak exposes full DB access.
- **Doc risk:** Architecture audit markdown desynced from code (B6); several "future implementation" stubs.

---

# 19 FRONTEND REBUILD GUIDE (based ONLY on current backend)

**Structure.** A Next.js app consuming:
1. Auth layer — Google OAuth → store JWT; attach `Authorization` header; pass `?token=` to WS.
2. Workspace/Server browsers — `GET /api/workspaces`, `/api/servers`.
3. Chat view — `GET/POST /api/chat/{workspace_id}`; render assistant/user messages.
4. Job runner — `POST /api/agents/forge-v2/run`; open `WS /api/v1/ws/jobs/{id}?token=`.
5. **Clarification renderer** — on `waiting_for_clarification` event, render `clarification_form` generically (title/description/questions/choices/validation). Collect answers → `POST /api/agents/jobs/{id}/clarification-reply` with `clarification_submission`. NEVER parse free text.
6. **Approval renderer** — on approval-suspend event, render `ApprovalRequest` and submit decision via the wake endpoint.
7. Job status/progress — WS events + `GET /api/agents/forge-v2/jobs/{id}`, `/api/jobs/{id}/steps`, `/timeline`.

**Stable contracts (depend on today).**
- JWT auth (`core/security.py`), Google OAuth (`/api/auth/google`).
- Job run + WS events (`routers/ws.py`, `agent_service._publish`).
- `ClarificationForm` / `ClarificationFormSubmission` schema (`models/clarification_form.py`) — generic, secret-safe.
- REST CRUD: workspaces, servers, chat, deployments, jobs.

**Unstable contracts (avoid or verify first).**
- `/forge-v2/ws/{id}` — **stub, do not use**.
- `JobQueue` semantics — irrelevant (no-op).
- Approval-submit exact endpoint — trace `approval_engine.py` + `routers/agents.py` before building.
- Resume cursor — not implemented; expect full re-run on resume (B4).

**Dynamic (never hardcode).**
- Clarification question types (`ClarificationQuestionType` enum — 17 types).
- Question choices, validation, placeholders, examples (all backend-supplied).
- Approval risk levels / types.
- Intent (`code`/`chat`/`server`) and `AgentJobStatus` values.

**Never hardcode.** Project types (e.g. "Telegram bot"), Telegram logic, specific server platforms — the form/flow is generic. Secrets must never be echoed in the UI.

---

# 20 PRODUCTION READINESS SCORE

| Subsystem | Score | Explanation |
|---|---|---|
| Authentication | 8 | JWT + Google OAuth, WS token auth; solid. |
| Authorization | 7 | `PermissionService` single gate; RLS helpers. Service-role key risk if leaked. |
| Workers | 5 | In-process; `JobQueue` stub; no distributed queue. |
| Job Queue | 2 | No-op stub (`job_queue.py`). Claiming works via DB atomic UPDATE only. |
| Planner | 8 | `build_plan` active, emits plan. |
| Execution | 7 | `executor.py` tool loop + self-healing works; B4 resume re-runs. |
| Clarification | 6 | Generic form excellent, but B1 disables discovery→adaptive for fresh jobs. |
| Approval | 8 | Per-step gate + suspend works. |
| Conversation | 7 | Persistence + reliability guards present. |
| Implementation Intelligence | 8 | `decide_strategy` + templates active. |
| Deployment | 5 | curl verification only in forge-v2; PM2 not wired (B5). |
| WebSocket | 6 | `/v1/ws` works; `/forge-v2/ws` stub; Redis pub/sub no-persist. |
| Frontend Contract | 7 | Mostly stable; approval-endpoint + stub WS need care. |
| Documentation | 4 | Architecture audit desynced (B6); stubs undocumented. |
| Testing | 6 | 22 test files; clarif/resume covered; B1/B2 not caught by tests. |
| Event Wait (crash-safety) | 3 | In-memory waiters; not restart-durable (B3). |
| Suspend/Resume durability | 3 | B3 + B4. |
| Secrets handling | 8 | Redaction + service-role isolation; at-rest encryption NOT VERIFIED. |

**Overall:** ~6/10. Functionally complete for happy-path execution; weak on crash-safety, queueing, and the silent discovery/clarification disable (B1).

---

# 21 FINAL ARCHITECTURE DIAGRAM

See the ASCII graph in §10 (event graph) and §11 (call graph). Summary:

```
                ┌─────────────┐
  client ──────▶│ routers/*   │ (auth, ws, gateway, agents, jobs, chat, workspaces, servers, deployments)
                └──────┬──────┘
                       │ submit_job
                ┌──────▼──────┐
                │ AgentService│ run_agent_pipeline (orchestration hub)
                └──────┬──────┘
          ┌────────────┼───────────────────────────────┐
          ▼            ▼                                 ▼
   Requirement     Adaptive Clarification          Intent → code/chat/server
   Discovery(B1)   (B1) → ClarificationForm         │
          │            │  suspend → EventWaitEngine    ├─ ImplementationIntelligence
          │            │  (in-memory, B3)              ├─ Planner.build_plan
          │            ▼                               ├─ Executor (SSH, self-heal)
          │      await_clarification_reply            └─ Deployment (curl, B5)
          │            │  fold → re-enter
          └─────── Approval (per-step suspend) ◀── on_step_start
                       │
                  Completion → _db_update + _publish
                       │
                  WorkerService (lifespan) claims from DB
                       │
                  Redis pub/sub → WS (routers/ws.py) → client
```

**Dependency graph (high level).**
`routers → AgentService → {discovery, clarification, approval, impl-intel, planner, executor, ssh, chat, workspace, server, permission, guardrails, event_wait, interactive_wait, constitution} → Supabase + Redis + asyncssh`.

---

# 22 FINAL SUMMARY

**What exists.** A full agent backend: orchestration, discovery, clarification (now generic + secret-safe), approval, implementation intelligence, planner, executor (SSH + self-heal), deployment verification, conversation memory, event-driven suspend/resume, WebSocket streaming, gateway, auth, permissions, reliability guards, and a 26-table Supabase schema.

**What is missing.** A real distributed job queue (`JobQueue` is a stub). Crash-durable suspend/resume (state is in-memory). PM2 wiring in forge-v2 (B5). Verification of secret at-rest encryption. A working `/forge-v2/ws` (stub).

**What is stable.** Auth (JWT/OAuth), WS event streaming (`/v1/ws`), `ClarificationForm` schema, REST CRUD, planner, executor happy-path, approval gate.

**What is experimental.** `DecisionRouter`/`Shadow`/`Weighted` (feature-flagged). `progressive_context`, `project_brain` memory layer. `resume_outcomes`/`agent_context_logs` persistence. `conversation_reliability` guards.

**What frontend can safely depend on today.** JWT auth; `POST /api/agents/forge-v2/run`; `WS /api/v1/ws/jobs/{id}`; `ClarificationForm`/`ClarificationFormSubmission` contract; approval gate; job status endpoints; workspace/server/chat/deployment CRUD. **Avoid:** `/forge-v2/ws` (stub), `JobQueue` semantics, assuming crash-safe resume, hardcoding project types.

**CRITICAL for any frontend rebuild:** fix **B1/B2** first — until then, Requirement Discovery and Adaptive Clarification are silently disabled for new jobs, so the structured clarification form (the centerpiece of the prior sprint) never triggers in production.
