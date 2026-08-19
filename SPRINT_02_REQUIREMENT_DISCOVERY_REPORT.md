# ThinkSync — Sprint 2: Requirement Discovery Engine

**Date:** 2026-07-04
**Sprint Goal:** Ensure the agent fully understands the user's project before planning or coding. Never start generating code with missing critical information.

---

## Sprint Goal

Add a **Requirement Discovery Engine** that runs once per conversation (before planning) and produces a structured `ProjectSpecification`. This spec becomes the source of truth for the planner and executor, eliminating repeated missing-information guessing across steps.

**Constraints honored:**
- No new features (the engine produces an internal spec — it does not change the user-facing API)
- No architecture redesign
- No deployment changes
- No template logic changes
- No approval flow changes

---

## Problems Solved

| # | Problem | Impact |
|---|---|---|
| 1 | Agent starts generating code with missing critical info (framework, auth, DB, secrets) | Wasted tokens, broken code, user frustration |
| 2 | Planner has no project context — LLM guesses framework/language every step | Inconsistent plans, wrong tool choices |
| 3 | No structured spec — every step re-infers basic facts | Redundant LLM calls, slower execution |
| 4 | Secrets/tokens guessed or invented | Security risk, broken deployments |
| 5 | Deployment target unknown — agent guesses ``localhost`` or random port | Deployment failures |

---

## Architecture Before

```
User Request
  ↓
classify_intent()        ← LLM call (no project context)
  ↓
detect_task_mode()       ← LLM call (no project context)
  ↓
build_plan()             ← LLM call (no project context)
  ↓
execute steps             ← agent discovers missing info via trial-and-error
```

**Problems:**
- No project context at planning time
- LLM guesses framework, language, auth needs
- Secrets/tokens are invented if not provided
- Deployment target is assumed

---

## Architecture After

```
User Request
  ↓
should_run_discovery()?   ← heuristic gate (no LLM call)
  │
  ├── NO  → skip (chat / debug / patch / existing workspace)
  │
  └── YES ↓
      │
      run_discovery()        ← LLM call: produce ProjectSpecification
      ↓
      spec cached in Redis   ← key: req_discovery:spec:{conversation_id}
      ↓
classify_intent()          ← NOW has spec context (via conversation_history)
  ↓
detect_task_mode()         ← NOW has spec context
  ↓
build_plan(…, project_spec=spec)
  ↓                        ← spec injected into LLM context as
  │                           "project_specification" dict
execute steps             ← agent uses spec (framework, auth, DB, secrets
      ↓                       are known; no guessing)
```

**Improvements:**
- Planning LLM sees `project_specification` in its context → no more guessing
- `missing_info` list is visible to planner → risk notes emitted
- `secrets_required` list is explicit → agent asks user (via event) instead of inventing values
- `deployment_target` is explicit → port/subdomain allocation is correct
- Spec is cached per `conversation_id` → survives worker restarts, used by all subsequent jobs in the conversation

---

## Discovery Flow

### Step 1 — Heuristic Gate (`should_run_discovery`)

Runs **without an LLM call**. Returns `False` (skip) for:

- `intent == "chat"` (normal conversation)
- Existing workspace (continuation, not new project)
- Objective contains debug/patch keywords (`traceback`, `error`, `fix`, `patch`, `debug`)
- `intent == "server"` + admin keywords (`restart`, `status`, `log`)

Returns `True` for new project requests.

### Step 2 — Initial Spec Extraction (`run_discovery`)

Calls the LLM with a structured prompt that asks for JSON matching `ProjectSpecification` schema. The prompt includes:

- The user's objective
- The last 10 conversation messages (for context)

The LLM returns a JSON dict with fields: `project_type`, `framework`, `language`, `features`, `auth_required`, `database_required`, `external_services`, `secrets_required`, `deployment_target`, `production_ready`, `missing_info`, `confidence`.

### Step 3 — Spec Caching

The spec is persisted in Redis with key `req_discovery:spec:{conversation_id}` (TTL = 6 hours). This means:

- The spec survives worker restarts
- Subsequent jobs in the same conversation reuse the spec (no re-discovery)
- The spec is available to all workers (not just the one that ran discovery)

### Step 4 — Spec Injection into Planning

`build_plan()` now receives `project_spec=spec` and injects it into the LLM context dict as `"project_specification"`. The planner LLM can now see:

```json
{
  "project_specification": {
    "project_type": "web_app",
    "framework": "fastapi",
    "language": "python",
    "features": ["user auth", "REST API", "PostgreSQL"],
    "auth_required": true,
    "database_required": true,
    "external_services": ["openai"],
    "secrets_required": ["OPENAI_API_KEY", "DATABASE_URL", "JWT_SECRET"],
    "deployment_target": "thinkync_server",
    "production_ready": false,
    "missing_info": ["JWT_SECRET value not provided"],
    "confidence": 0.75
  }
}
```

### Step 5 — Non-Blocking Clarification (Current Limitation)

The current architecture is job-based (not interactive within a single job). The engine does NOT block for user replies in this sprint. Instead:

- If `missing_info` is non-empty, the engine sets `spec.needs_user_input = True`
- The spec (with `missing_info` listed) is passed to the planner
- The planner emits risk notes
- A future sprint can make this interactive by splitting the job into discovery + wait + planning phases

---

## Specification Lifecycle

```
User sends "build me a fastapi app with auth"
  ↓
should_run_discovery() → True (new project, no workspace)
  ↓
run_discovery() → LLM produces spec
  │              confidence=0.6, missing_info=["JWT secret value"]
  ↓
spec cached in Redis
  ↓
build_plan(…, project_spec=spec)
  ↓                        ← LLM sees spec, plans with
  │                          FastAPI + JWT (not Flask + random auth)
execute steps
  ↓
Next user message in same conversation
  ↓
should_run_discovery() → False (spec already cached)
  ↓                        ← no re-discovery, uses cached spec
build_plan(…, project_spec=cached_spec)
```

**Cache invalidation:** `clear_cached_spec(conversation_id)` is available for when the user explicitly starts a new project.

---

## Files Modified

| File | Changes |
|------|---------|
| `backend/models/agent.py` | Added `ProjectSpecification` pydantic model |
| `backend/models/job.py` | Added `conversation_id: str | None` to `JobCreate` |
| `backend/services/requirement_discovery.py` | **NEW FILE** — the discovery engine |
| `backend/services/agent_service.py` | Hook discovery into `run_agent_pipeline`; pass `project_spec` to `build_plan()` |
| `backend/services/planner.py` | Added `project_spec` parameter to `build_plan()`; inject spec into LLM context |

---

## Remaining Limitations

| Limitation | Reason | Recommended Sprint |
|-----------|--------|-------------------|
| Discovery is **non-blocking** — user replies to clarifying questions are not waited for | Job-based architecture (single job = single response) | Sprint 3 (Interactive Wait) |
| `conversation_id` falls back to `job_id` if not provided by the client | Client may not send `conversation_id` in `JobCreate` | Sprint 3 (Client API update) |
| `specification` column may not exist in `jobs` DB table (the `_db_update` call writes to it) | DB migration not run | **Action required:** run `ALTER TABLE jobs ADD COLUMN IF NOT EXISTS specification jsonb;` |
| Discovery runs once per conversation, not once per *project* | No notion of "project" in the data model — conversation = project | Sprint 4 (Project model) |
| `missing_info` items are listed but not shown to the user in a structured way | No frontend component for discovery questions yet | Sprint 3 (Frontend integration) |

---

## Sprint Summary

**What was added:**
- `ProjectSpecification` model — 13 fields covering project type, framework, language, features, auth, DB, external services, secrets, deployment target, production readiness, missing info, confidence
- `requirement_discovery.py` — the engine with `should_run_discovery()` heuristic gate and `run_discovery()` LLM-based spec extraction
- Integration into `run_agent_pipeline()` — discovery runs before intent classification
- Integration into `build_plan()` — spec is injected into LLM context

**What was NOT changed:**
- No new API endpoints
- No changes to the execution pipeline
- No changes to templates
- No changes to approval flow
- No changes to deployment

**Production readiness:**
- The engine is **opt-in** per conversation (heuristic gate)
- If the engine fails (LLM error, etc.), the pipeline proceeds without it (`except Exception: pass`)
- The engine does NOT block job execution
- The engine does NOT invent secret values (it lists them in `secrets_required` and leaves values empty)
