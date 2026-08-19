# Forensic Architecture Audit of the AI Agent

## Critical Findings

- `backend/services/agent_service.py:1121-1210` has a runtime bug: `run_agent_pipeline()` evaluates `if intent == "chat":` before `intent` is assigned. The assignment happens later at `intent = (await agent_llm.classify_intent(...))`.
- `backend/services/tools.py:1329-1330`, `backend/services/tools.py:1741-1748`, `backend/services/agent_service.py:1153-1155`, `backend/services/agent_service.py:1688-1690`, `backend/services/agent_service.py:569-571`, and `backend/services/tools.py:197-199` show write permission is forcibly overridden to `True` in multiple places. `get_tool_definitions()` also ignores the caller’s `allow_write` flag and only filters `DEPLOY_NEXTJS_APP`.
- `backend/main.py:115-131` starts health and recovery loops only. A worker process exists in code, but worker startup in deployment is `NOT VERIFIED` from this repository.

## 1. Agent Architecture Map

- `POST /jobs` in [backend/routers/jobs.py](backend/routers/jobs.py) and `POST /agents/forge-v2/run` in [backend/routers/agents.py](backend/routers/agents.py) create jobs via `AgentService.submit_job()` and `AgentService.create_job()` in [backend/services/agent_service.py](backend/services/agent_service.py).
- `JobQueue.enqueue_job()` in [backend/services/job_queue.py](backend/services/job_queue.py) writes queue state to Redis; this is the only queue enqueue path in the routers.
- `WorkerService.run()` in [backend/services/worker_service.py](backend/services/worker_service.py) claims jobs with `_claim_next_job()` and executes them via `_execute_job()`.
- `_execute_job()` calls `AgentService.run_job()` in [backend/services/agent_service.py](backend/services/agent_service.py), which wraps `_run_agent_loop()`, which wraps `run_agent_pipeline()`.
- `run_agent_pipeline()` in [backend/services/agent_service.py](backend/services/agent_service.py) performs request state checks, intent detection, task-mode detection, planning, and dispatch to either `_run_code_execution()` or `run_server_execution()`.
- For code requests, `_run_code_execution()` in [backend/services/agent_service.py](backend/services/agent_service.py) does workspace resolution, context building, template matching, patching, validation, and self-healing.
- For server requests, `run_server_execution()` in [backend/services/executor.py](backend/services/executor.py) loads `WorkspaceContext`, checks tool discipline, executes tools through `execute_tool()`, validates results, retries failures, and emits execution events.
- Deployment and exposure run through `DeploymentService.create_deployment()` in [backend/services/deployment_service.py](backend/services/deployment_service.py) and `DeployService.expose_workspace_subdomain()` in [backend/services/deploy_service.py](backend/services/deploy_service.py).
- Health and recovery loops are started from `backend/main.py`, but worker startup is not.

## 2. Layer Inventory and Quality Score

| Layer | Status | Score | Evidence |
|---|---:|---:|---|
| Prompt Builder | EXISTS | 7/10 | `agents/constitution.py::build_prompt`, used by `agent_llm` |
| Intent Detection | EXISTS | 6/10 | `agent_llm.classify_intent_with_confidence`, `classify_intent`, `fallback_intent` |
| Planner | EXISTS | 7/10 | `planner.build_plan`, `agent_llm.generate_plan`, `agent_llm.revise_plan` |
| Memory | EXISTS | 6/10 | `memory.py::MemoryStore`, `ChatService` usage in `agent_service.py` |
| Context Builder | EXISTS | 7/10 | `ContextEngine.build_context`, indexing, snippet extraction, Redis cache |
| Tool Selection | PARTIAL | 4/10 | `tools.py::get_tool_definitions()` ignores `allow_write`; write-only tools still exposed |
| Execution Engine | EXISTS | 8/10 | `executor.py::run_server_execution`, `_run_validated_step`, retry/validator flow |
| SSH | EXISTS | 6/10 | `SSHService.execute()`, `validate_server_connection()` are real; host key checking disabled |
| Deployment | PARTIAL | 5/10 | PM2/nginx deployment exists, but worker launch and rollback are not verified |
| Patch Engine | EXISTS | 8/10 | `agent_llm.run_safe_patch_edit()`, `guardrails.apply_text_patches()`, `validate_patched_files()` |
| Verification | EXISTS | 8/10 | `validate_python_syntax()`, curl checks, executor validators |
| Retry | EXISTS | 7/10 | executor validator retries, self-healing retries, patch retries |
| Recovery | PARTIAL | 5/10 | `JobRecovery`, `WorkerService.recover_stale_jobs()`, resume not implemented |
| Rollback | MISSING | 0/10 | No runtime rollback path found; only retry/release logic exists |
| Logging | EXISTS | 7/10 | `obs.emit`, `append_run_log`, event streams |
| Monitoring | EXISTS | 6/10 | `health_checker.run_health_check_loop()`, event persistence, workspace health marks |
| Context Engine | EXISTS | 7/10 | `ContextEngine` indexes files, selects snippets, caches context |
| Reflection | PARTIAL | 5/10 | `evaluate_step()`, `revise_plan()`, failure history, but no general reflection loop |
| Self-Healing | EXISTS | 6/10 | `self_healing.execute_with_self_healing()` retries and repairs runtime errors |
| Job Queue | PARTIAL | 4/10 | queue and worker code exist, but startup wiring is not verified |
| Port Allocation | EXISTS | 8/10 | Redis-backed allocator with consistency repair |
| Workspace Resolution | EXISTS | 7/10 | `WorkspaceService.resolve_workspace()`, `create_workspace_from_prompt()` |
| Subdomain | EXISTS | 6/10 | `DeployService.expose_workspace_subdomain()`, `capability_service.load_workspace_context()` |
| Health Check | EXISTS | 7/10 | `health_checker.run_health_check_loop()`, startup consistency check |
| Security | PARTIAL | 2/10 | command/path guards exist, but write gating and SSH host key checks are bypassed |
| Learning | PARTIAL | 4/10 | patch strategy stats exist; no general adaptive learning loop |

## 3. Quality Score

- The table above is the scorecard. Lowest scores are `Rollback`, `Security`, `Job Queue`, `Tool Selection`, and `Recovery`.
- The strongest areas are `Execution Engine`, `Patch Engine`, `Verification`, and `Port Allocation`.
- `NOT VERIFIED` items materially lower confidence: worker launch, rollback, and any deployment-layer durability beyond code presence.

## 4. Execution Trace

- Example request: `Create Telegram Bot`.
- `backend/routers/agents.py::forge_v2_run_async()` or `backend/routers/jobs.py::submit_job()` accepts the request.
- `backend/services/agent_service.py::AgentService.submit_job()` validates the workspace/server relationship.
- `backend/services/agent_service.py::AgentService.create_job()` persists the job row.
- `backend/services/job_queue.py::JobQueue.enqueue_job()` marks the job pending in Redis.
- If a worker is running, `backend/services/worker_service.py::WorkerService.run()` claims the job with `_claim_next_job()`, then `_execute_job()` calls `AgentService.run_job()`.
- `AgentService.run_job()` enters `_run_agent_loop()`, then `run_agent_pipeline()`.
- `run_agent_pipeline()` calls `agent_llm.classify_intent()`, then `agent_llm.detect_task_mode()`.
- For `Create Telegram Bot`, the code path is `intent == "code"` and then `_run_code_execution()`.
- `_run_code_execution()` calls `WorkspaceService.resolve_workspace()` or `create_workspace_from_prompt()`, `ContextEngine.build_context()`, `match_template()`, `render_template()`, `validate_code()`, `write_workspace_file()`, `validate_python_syntax()`, and `self_healing.execute_with_self_healing()`.
- `self_healing.execute_with_self_healing()` calls `universal_execute_python()`, which calls `exec_in_workspace()` and `SSHService.execute()`.
- If template matching fails, `_run_code_execution()` falls back to `agent_llm.generate_code_response()` and then the same validation/execution chain.
- Worker launch from deployment is `NOT VERIFIED`, so this trace is code-complete but runtime-complete only if an external worker process exists.

## 5. Template System

- Template search exists in `backend/services/templates.py::match_template()` and is keyword-based.
- Creation without a template exists: `_run_code_execution()` logs `template_no_match` and falls back to `ContextEngine.build_context()` and `agent_llm.generate_code_response()`.
- Template modification exists only as parameter substitution in `render_template()`.
- Requirement merging is `NOT VERIFIED`; there is no merge engine beyond static parameters and baseline execution steps.
- Template rewrite is `NOT VERIFIED`; `render_template()` only performs string replacement.
- Fallback exists: template no-match branches to LLM code generation.

## 6. Patch System

- File editing exists via `backend/services/tools.py::write_workspace_file()` and the patch path in `_run_code_execution()`.
- Structured patch application exists via `backend/services/guardrails.py::apply_text_patches()`.
- Patch verification exists via `backend/services/guardrails.py::validate_patched_files()`.
- Failed patch detection exists: `apply_text_patches()` returns `errors`/`error_details`, and `run_safe_patch_edit()` retries on mismatch.
- Rollback is missing. The patch flow retries with new constraints and releases the processing lock, but there is no revert-to-original-file path.
- Unified diff generation is `NOT VERIFIED`; this code uses target/replacement patch objects, not unified diff output.

## 7. Verification System

- Code execution exists via `backend/services/tools.py::exec_in_workspace()` and `backend/services/tools.py::universal_execute_python()`.
- Stdout/stderr capture exists because `SSHService.execute()` returns both and `exec_in_workspace()` exposes them.
- Error understanding exists in `backend/services/self_healing.py::parse_error()`.
- Error repair exists in `self_healing.execute_with_self_healing()`.
- Retry exists in both `executor.py` validator retries and `self_healing.py` repair retries.
- Health check exists in `backend/services/health_checker.py::run_health_check_loop()`.
- Deployment verification exists via `backend/services/deployment_service.py::create_deployment()` using `pm2 describe` and `curl -f`.
- Python syntax verification exists via `backend/services/guardrails.py::validate_python_syntax()`.

## 8. Deployment System

- Build/install is PARTIAL. `backend/services/tools.py::install_python_deps()` and `universal_execute_python()` can install Python dependencies, but `DeploymentService.create_deployment()` itself does not build an app.
- Run exists via PM2 in `DeploymentService.create_deployment()`.
- Restart exists via `tools.py::_restart_service()` and the PM2 restart branch in `DeploymentService.create_deployment()`.
- Rollback is missing. On failure the code releases the port, but does not restore the previous PM2 process, Nginx config, or Redis state.
- Failed deployment detection exists: PM2 describe checks and `curl -f` verification are explicit.
- Unhealthy deployment detection exists in `backend/services/health_checker.py`.
- `DeployService.expose_workspace_subdomain()` is Nginx reverse-proxy exposure, not app deployment.
- `tools.py::_deploy_nextjs_app()` is disabled, so the named deployment tool is not actually implemented.

## 9. Intent System

- Intent classification is real in `agent_llm.classify_intent_with_confidence()` and `classify_intent()`.
- Confidence score exists as a `0.0-1.0` float and is thresholded at `0.80` in `classify_intent()`.
- Fallback exists: low-confidence LLM output falls back to keyword routing via `fallback_intent()`.
- Multiple intents are not preserved; the system always resolves to one of `chat`, `code`, or `server`.
- Ambiguous requests are handled by fallback routing and regex overrides in `run_agent_pipeline()`.
- There is a hard execution gate in `agent_llm.run_tool_calling_loop()` and `tools.execute_tool()` that rejects non-`server` intents for tool execution.

## 10. Reasoning

- The agent plans. `planner.build_plan()`, `agent_llm.generate_plan()`, `agent_llm.revise_plan()`, and `executor.run_server_execution()` are real planning flows.
- The agent also executes directly for code and explicit-mode requests via `_run_code_execution()` and `run_explicit_mode()`.
- Task decomposition exists in `generate_plan()` and `run_server_execution()`.
- Prioritization exists only in narrow forms: task-mode classification, template keyword matching, and context file ranking.
- Reflection exists in step evaluation and revision, but there is no general self-critique loop beyond execution retries and patch-learning stats.

## 11. Memory

- Short-term memory exists via `backend/services/memory.py::MemoryStore`, stored in Redis.
- Workspace-scoped conversation memory exists via `ChatService.get_recent_context_messages()` and `save_workspace_message()`.
- File memory exists via `ContextEngine` workspace indexing and snippet extraction.
- Context memory exists via Redis cache keys in `ContextEngine.build_context()`.
- Long-term memory is `NOT VERIFIED`; the code shows Redis and DB-backed histories, not durable semantic memory.
- Workspace memory is partial because context is reconstructed from Redis/DB/files instead of a single canonical store.

## 12. Reliability

- Timeout handling exists in SSH execution, LLM calls, validator loops, and self-healing.
- Retries exist in patch repair, validator retries, and self-healing retries.
- Concurrency control exists as an in-process semaphore in `AgentService.run_job()`.
- Race conditions are partially handled with Redis locks, job claim claims, and patch processing locks.
- Queue reliability is partial because the queue state exists, but worker startup is not verified.
- Recovery exists in `JobRecovery` and `WorkerService`, but resume support is missing.
- Idempotency exists in port allocation and some deployment checks, but not universally.

## 13. Security

- SSH safety is partial. `SSHService.execute()` and `validate_server_connection()` use `known_hosts=None`, which disables host-key verification.
- Command safety is partial. `tools.py` has blocked patterns and path restrictions, but write access is forcibly enabled in multiple call paths.
- Sandbox is not present as a code-level runtime boundary; execution is remote SSH-based.
- Prompt injection defenses are `NOT VERIFIED`; there is a constitution prompt system, but no explicit injection filter or taint tracking.
- Secrets handling exists in `server_service.py` encryption/decryption and `main.py` redaction, but logs still carry broad execution metadata.
- Path traversal protection exists in `validate_workspace_path()`, `_validate_relative_path()`, and `_scope_workspace_command()`.
- Remote execution is deliberate and pervasive via `SSHService.execute()`.

## 14. Dead Code

- `backend/services/ai_service.py` appears unused. Repository-wide search found no imports or call sites.
- `backend/services/agent_llm.py.bak` is a backup copy, not part of runtime imports.
- `backend/services/executor.py.orig` is a backup copy, not part of runtime imports.
- `backend/services/server_service.py` duplicates `WorkspaceContext`, `detect_capabilities()`, and `load_workspace_context()` from `backend/services/capability_service.py`. The active agent imports `capability_service`, not the duplicate logic.
- `background_tasks` parameters in `backend/routers/jobs.py::submit_job()` and `backend/routers/agents.py::forge_v2_run_async()` are unused.
- `forge-v1` routes in `backend/routers/agents.py` are intentionally disabled with 410 responses; they are inactive surfaces, not runtime paths.

## 15. Technical Debt

1. Critical: `run_agent_pipeline()` uses `intent` before assignment.
2. Critical: write permission is forcibly enabled in multiple agent and tool paths.
3. Critical: `get_tool_definitions()` ignores `allow_write`.
4. Critical: SSH host key verification is disabled with `known_hosts=None`.
5. Critical: worker startup is not wired in `main.py`.
6. High: no rollback path for patching.
7. High: no rollback path for deployment.
8. High: `DeployService.expose_workspace_subdomain()` writes live Nginx config without compensating rollback.
9. High: `tools.py::_deploy_nextjs_app()` is disabled.
10. High: `server_service.py` duplicates platform context/capability logic.
11. High: `ai_service.py` is unused.
12. High: `JobQueue.enqueue_job()` is a Redis hash marker, not a durable consume queue.
13. High: `JobRecovery` and `WorkerService` both implement recovery logic with overlapping responsibilities.
14. Medium: `MemoryStore` is Redis-only best-effort memory, not durable long-term memory.
15. Medium: template matching is keyword-only and shallow.
16. Medium: template rendering only substitutes tokens; no requirement merge or rewrite engine.
17. Medium: context indexing has hard caps and may miss large workspaces.
18. Medium: `run_safe_patch_edit()` caps retries to 2 in practice.
19. Medium: `validate_patched_files()` only checks syntax, presence, and unintended changes.
20. Medium: `ExecutionRepository` dual-writes normalized tables and JSONB caches, which can diverge.
21. Medium: `ExecutionEventService.emit()` is best-effort and swallows DB/Redis failures.
22. Medium: `health_checker` only probes loopback HTTP endpoints.
23. Medium: `DeploymentService.create_deployment()` is PM2-centric and not app-type generic.
24. Medium: `run_explicit_mode("plan")` returns chat, not a structured plan.
25. Medium: `classify_intent()` collapses low-confidence cases to keyword fallback only.
26. Low: `audit_background_tasks()` is a static diagnostic report, not runtime enforcement.
27. Low: `JobQueue` concurrency is local unless worker deployment is real.
28. Low: `ContextEngine` fallback to Supabase index is partial when SSH scan fails.
29. Low: `main.py` HTTP logging captures request/response bodies with redaction, but broad bodies can still be logged.
30. Low: `forge-v1` code remains present but is disabled, increasing surface area.

## 16. Capability Matrix

| Capability | Score | Evidence |
|---|---:|---|
| Planning | 7/10 | `generate_plan()`, `build_plan()`, `revise_plan()` |
| Coding | 8/10 | `_run_code_execution()`, `generate_code_response()` |
| Editing | 8/10 | `write_workspace_file()`, `apply_text_patches()` |
| Refactoring | 5/10 | patching exists, but no true semantic refactor loop |
| Debugging | 8/10 | validators, error parsing, self-healing |
| Deployment | 6/10 | PM2 deploy + nginx exposure + checks |
| Monitoring | 7/10 | health checker, event stream, logs |
| Recovery | 5/10 | `JobRecovery`, `WorkerService.recover_stale_jobs()` |
| Rollback | 1/10 | no rollback code found |
| Verification | 8/10 | `validate_python_syntax()`, curl/PM2 verification |
| Template Adaptation | 6/10 | `render_template()`, `template_execution_hint()` |
| Context Awareness | 7/10 | `ContextEngine`, `load_workspace_context()` |
| Reasoning | 6/10 | intent/task-mode planning and evaluator loops |
| Learning | 4/10 | patch strategy stats exist only in patch pipeline |
| Self-Healing | 6/10 | `execute_with_self_healing()` |
| Memory | 6/10 | Redis memory + chat history + context cache |

## 17. Production Readiness

| Dimension | Score | Evidence |
|---|---:|---|
| Architecture | 5/10 | real layers exist, but duplicated and partially wired |
| Reliability | 3/10 | runtime bug, queue/worker launch gap, no rollback |
| Maintainability | 4/10 | separation exists, but duplicate modules and forced write overrides |
| Extensibility | 5/10 | planner/executor/tool registry are extensible |
| Observability | 7/10 | logs, events, health checks, execution history |
| Developer Experience | 5/10 | many diagnostics exist, but multiple code paths are easy to miswire |
| Security | 2/10 | write gating bypass + SSH host key checks disabled |
| Scalability | 3/10 | in-process semaphore and unverified worker launch limit confidence |
| Deployment | 3/10 | PM2/nginx code exists, but rollback and worker launch are incomplete |
| Overall Production Readiness | 4/10 | code exists for most agent subsystems, but there are blocking safety and wiring defects |

## 18. Final Verdict

- `Early Beta`
