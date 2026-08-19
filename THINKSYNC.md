# THINKSYNC.md — Engineering Memory (AI-only)

> This file is **NOT** a README. It exists only for AI agents working on
> ThinkSync. It is the persistent **Project Brain**: structured, long-term
> engineering knowledge that evolves incrementally over the project lifetime.
>
> **Update policy:** Never rewrite this file entirely. Append / patch only the
> sections that changed (see Context Diff Engine). Preserve manual notes.

---

## 1. Product & Mission

ThinkSync is an autonomous agent platform that turns natural-language
objectives into running applications on remote servers. A user describes what
they want; the agent scaffolds, implements, deploys, monitors and self-heals a
deployed service.

Stated value (from marketing copy): *"Think it. Sync it. Ship it."*

---

## 2. Technology Stack

- **Backend:** Python (FastAPI-style), Supabase (Postgres + RLS) as primary store.
- **Async runtime:** `asyncio`; SSH execution against remote servers.
- **LLM:** OpenAI-compatible endpoint (configurable `OPENAI_BASE_URL`, default
  SiliconFlow `gpt-4o-mini`).
- **Cache / ephemeral memory:** Redis (Upstash), optional.
- **Frontend:** Next.js app under `frontend/app`.
- **Infra:** nginx, systemd, certbot, Supabase (local/cloud), under `infra/`.
- **DB migrations:** under `backend/db/migrations`.

---

## 3. Architecture (high level)

```
user objective
   → Approval / Clarification gate
   → Planner (steps)
   → Implementation Intelligence (template vs AI)
   → Context Engine (file selection + snippets)
   → Executor (SSH, self-healing)
   → Resume / InteractiveWait (pause for approval)
   → Deployment / Server services
```

Key service modules (`backend/services/`):
- `context_engine.py` — repository indexing + context/snippet selection. (PRESERVED)
- `implementation_intelligence.py` — template discovery/ranking/hybrid. (PRESERVED)
- `agent_service.py` — orchestration pipeline (huge; `build_context` call site).
- `resume_manager.py`, `interactive_wait.py`, `conversation_continuation.py`,
  `conversation_policy.py`, `conversation_audit.py` — conversation/resume layer.
- `approval_engine.py`, `approval_policy.py` — human-in-the-loop gate.
- `self_healing.py`, `executor.py`, `worker_service.py` — execution.
- `templates.py` — spec templates.

---

## 4. Coding Conventions

- `from __future__ import annotations` at top of every module.
- Static methods / classmethods preferred for stateless service logic.
- Typed exceptions over bare `except Exception` (audit rule).
- Supabase access via `core.database.get_supabase()`; settings via
  `core.config.get_settings()`.
- Backward compatibility is load-bearing: sprints extend, never redesign.
- Project venv: `/root/thinksync/backend/.venv/bin/python3` (NOT system python3).

---

## 5. Key Design Decisions (per Decision Memory)

<!-- DECISIONS: maintained by DecisionMemory. Append only. -->
- **Event Wait Engine** — jobs may suspend up to 30–60 min awaiting user
  approval; `WAIT_TIMEOUT_SECONDS` clamped to [1800, 3600].
- **Approval subsystem** — write actions gated by `AGENT_ALLOW_WRITE` +
  per-action allow lists; `APPROVAL_RESUME_SECRET` required in prod.
- **Context Engine cache-first** — Redis cache keyed by
  `context:{workspace_id}:{sha256(task)}`; falls back to Supabase index.

---

## 6. Current Sprint & Roadmap

- **Current:** Sprint 3C.E — Context Engineering (this sprint).
- **Goal:** continuous long-term engineering understanding with minimal token
  usage; Project Brain, THINKSYNC.md, Session Snapshot, progressive loading,
  repo index, decision/architecture/task memory, context budget, compression,
  freshness, confidence, diff engine, GC.

---

## 7. Known Limitations / Technical Debt

<!-- LIMITATIONS: maintained by ProjectBrain GC. -->
- Context Engine rescans repository on cache miss (mitigated by RepositoryIndex
  incremental refresh in this sprint).
- `agent_service.py` is a large monolith (`build_context` is one call site).
- Memory layer (conversation) is TTL-based Redis only.

---

## 8. Critical Workflows

- New task flow: `agent_service` → `ContextEngine.build_context` (PATCH/CREATE).
- Patch flow: load existing files via context engine → `agent_llm.run_safe_patch_edit`.
- Resume flow: `ConversationContinuationEngine.get_continuation_context`.

---

## 9. Security Decisions

- SSH strict host key checking in prod (`SSH_STRICT_HOST_KEY_CHECKING`).
- Approval gate is the production kill-switch for write actions.
- Sensitive data may be encrypted at rest via `DATA_ENCRYPTION_KEY` (Fernet).

---

## 10. Production Constraints

- `AGENT_ALLOW_WRITE` global kill-switch.
- Wait windows bounded to 30–60 min.
- Context budget limits: `AGENT_CONTEXT_MAX_FILES=3`,
  `AGENT_CONTEXT_MAX_TOTAL_LINES=260`, `AGENT_CONTEXT_MAX_LINES_PER_FILE=120`.

---

<!-- SESSION_SNAPSHOT: maintained by SessionSnapshot. Replaced each session end. -->
## Session Snapshot

- **Goal:** Implement Sprint 3C.E Context Engineering.
- **Completed:** Existing-system audit; architecture report.
- **Blockers:** none.
- **Open questions:** how aggressively to auto-write THINKSYNC.md from CI-less env.
- **Next step:** Build `project_brain.py` + `context_memory.py` + `context_budget.py`
  + `repository_index.py` + `progressive_context.py`; wire into orchestration.


## Known Limitations / Technical Debt

stack
- workspace_awareness: - Recent changes: a.py


## Current Sprint & Roadmap

stack
- **Current:** Sprint 3C.E — Context Engineering.

