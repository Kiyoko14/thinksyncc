# ThinkSync

> An autonomous agent platform — describe what you want in natural language, and **Forge v2** plans and executes multi-step operations on your remote servers, then deploys, monitors, and self-heals the running service.

ThinkSync turns infrastructure intent into shipped, running applications: connect SSH servers, spin up workspaces, run a constitutional AI agent that scaffolds/implements/deploys, and track every job with full audit logging and live event streaming.

> **"Think it. Sync it. Ship it."**

---

## Table of Contents

- [Architecture](#architecture)
- [Key Subsystems](#key-subsystems)
- [Tech Stack](#tech-stack)
- [Project Layout](#project-layout)
- [Authentication](#authentication)
- [API Overview](#api-overview)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Testing](#testing)
- [Security](#security)
- [Roadmap](#roadmap)

---

## Architecture

ThinkSync is a monorepo with a clean separation between modules:

```
thinksync/
├── backend/        FastAPI backend (Python 3.10+)
├── frontend/       Next.js 14 app (TypeScript, Tailwind CSS)
├── agents/         Agent Constitution & future standalone agents
├── infra/          nginx, docker-compose, Supabase schema
├── scripts/        Local dev & deployment helpers
└── MERGE_ANALYSIS/ Historical merge/audit working notes
```

### Backend (`backend/`)

Strict layered architecture — no cross-layer coupling:

| Layer | Responsibility |
|-------|----------------|
| `routers/` | HTTP parsing, response serialisation, route guards |
| `services/` | All business logic — `agent_service`, `workspace_service`, `server_service`, `executor`, `approval_engine`, `decision_*`, `event_wait_engine`, … |
| `models/` | Pydantic request / response schemas |
| `core/` | Config, security, database client, cryptography |
| `routers/ws.py` | WebSocket job-event streaming (`/v1/ws`) |

**Invariants:**

- Routers contain zero business logic — they delegate entirely to services.
- Services never import from routers.
- All configuration is sourced from environment variables via `core/config.py`.
- SSH credentials are encrypted at rest using Fernet symmetric encryption (`core/crypto.py`).

### Frontend (`frontend/`)

Next.js 14 App Router. Client-side route protection via a `localStorage` token check.

| Route | Description |
|-------|-------------|
| `/login` | Google sign-in (OAuth) |
| `/dashboard` | Home screen after login |
| `/servers` | Add, list, and delete servers |
| `/servers/[serverId]/workspaces` | Workspaces for a server |
| `/workspace/[workspaceId]/chat` | Chat with a workspace's agent |
| `/chat/[workspaceId]` | Alternate chat route |
| `/demo` | Live agent demo |

API calls are centralised in `services/api.ts` (typed fetch wrapper) and `services/auth.ts` (login / logout / token storage).

---

## Key Subsystems

### Forge v2 — the unified agent pipeline
Forge v1 endpoints (`/agents/forge-v1/*`, `/agents/forge/*`) are **disabled and return `410 Gone`**. All agent work goes through the unified Forge v2 pipeline:

- `POST /agents/forge-v2/plan` — generate an LLM execution plan without executing.
- `POST /agents/forge-v2/run` (202) — enqueue a job; poll `GET /agents/forge-v2/jobs/{job_id}`.
- `GET /agents/forge-v2/jobs/{job_id}` — poll job status & result.
- `WS /v1/ws/jobs/{job_id}` — live event stream (history replay + Redis Pub/Sub).

Jobs are persisted to the DB and consumed by a **background worker loop** (`services/worker_service.py`, started in `main.py` lifespan). Without it, queued jobs never advance past `queued`.

### Constitutional AI
Forge v2 operates under a strict **Constitution** (`agents/` + `services/constitution.py`) to prevent objective drift, enforce platform awareness (ports / subdomains / SSL loaded before execution), and gate success behind multi-stage verification.

| Principle | Enforcement |
|---|---|
| **Objective Adherence** | Actions continuously checked against the initial objective. |
| **Platform Awareness** | Platform context must be loaded before execution; failure → critical error. |
| **Success Contract** | Deployment marked successful only after local + gateway + public-URL checks. |
| **Runtime Integrity** | Redundant/contradictory actions (e.g. restarting a running service) are prevented. |
| **Tool Discipline** | Only a pre-approved tool set may run; arbitrary shell is blocked. |
| **Safety** | Dangerous commands blocked unless explicitly confirmed by the user. |

### Decision Engine (`DECISION_ENGINE_MODE`)
A tiered decision layer that can observe or drive agent routing:

| Mode | Behaviour |
|------|-----------|
| `off` | Current production behaviour; engine not computed. |
| `shadow` | Computes + records MATCH/MISMATCH only. |
| `weighted` | Computes a RECOMMENDATION + agreement/safety classification. |
| `authoritative` | Engine *selects* the route; legacy validation still vets it. Security gates remain absolute. |

Any unrecognized value clamps to `off` (fail-safe). The legacy `DECISION_ENGINE_SHADOW` bool resolves to `shadow` for backward compatibility.

### Approval Subsystem & write kill-switch
Write actions are gated by `AGENT_ALLOW_WRITE` (the **global production kill-switch**) plus per-action allow-lists. Resuming a suspended job requires `APPROVAL_RESUME_SECRET`, which is **mandatory in production** — the app fails fast at startup if it is missing.

### Event Wait Engine (no polling)
Jobs may **suspend** (up to 30–60 min, clamped via `WAIT_TIMEOUT_SECONDS`) while awaiting human input, then wake on a single signal — no polling loop:

- `POST /agents/jobs/{job_id}/reply` — a user reply (Telegram bridge / Web UI / API).
- `POST /agents/jobs/{job_id}/clarification-reply` — structured `ClarificationFormSubmission` (server-validated, HTTP 422 on invalid).
- `POST /agents/jobs/{job_id}/event` — generic system event (RESUME_REQUEST, CANCEL, webhooks, …).

### Context Engineering (Sprint 3C.E)
Budget-bounded context selection (`AGENT_CONTEXT_MAX_FILES`, `_MAX_TOTAL_LINES`, `_MAX_LINES_PER_FILE`), repository indexing, progressive loading, decision/architecture/task memory, and a `THINKSYNC.md` **Project Brain** for long-term engineering knowledge with minimal token usage.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | Python 3.10+, FastAPI, asyncssh >= 2.14.2 |
| Database | Supabase (Postgres + PostgREST) — used as **PostgreSQL only** (Supabase Auth disabled; identities live in `public.users`) |
| Auth | **Google OAuth** (ID-token exchange) + custom JWT (HS256) |
| Agents | OpenAI-compatible LLM (`resolve_model()` tiered roles); Constitutional AI framework |
| Cache / Events | Redis (Upstash), optional — Forge v2 LLM caching, job-event Pub/Sub, chat memory |
| Encryption | `cryptography` >= 42 — Fernet symmetric encryption |
| Proxy | nginx |
| Containers | Docker + Docker Compose |

> App version is the single source of truth in `backend/core/config.py` (`APP_VERSION`, currently `1.28.1`). The build/deploy env examples may lag — trust `core/config.py`.

---

## Authentication

ThinkSync uses **Google OAuth as the only authentication method**.

1. The browser obtains a Google ID token and POSTs it to `POST /auth/google`.
2. The backend verifies the token cryptographically against Google's public certs (`GOOGLE_CLIENT_ID`, `GOOGLE_ISSUERS`, `GOOGLE_CERTS_URL`).
3. The user is upserted into `public.users` (created on first login).
4. The backend issues a ThinkSync HS256 JWT whose `sub` is `public.users.id`.

Routes:

- `POST /auth/google` — exchange a verified Google ID token for a JWT.
- `GET /auth/me` — current user (protected).
- `POST /auth/logout` — client-side logout signal (the JWT is stateless; real invalidation is dropping the token client-side).

> Email/password auth and the Supabase Auth dependency that powered it were removed — see `SPRINT_OAUTH_MIGRATION_PLAN.md`.

---

## API Overview

Full interactive docs: `GET /docs` (only when `DEBUG=true`). The REST surface groups into:

| Area | Prefix | Highlights |
|------|--------|-----------|
| Health | `/health` | Liveness (no auth). |
| Auth | `/api/v1/auth` | Google OAuth exchange, `me`, logout. |
| Servers | `/api/v1/servers` | Add/list/delete SSH servers; connection validation at creation. |
| Commands | `/api/v1/commands` | SSH command execution → stdout/stderr/exit code/timing. |
| Workspaces | `/api/v1/workspaces` | Create (auto slug + subdomain), list, manage. |
| Chat | `/api/v1/chat` | Workspace + git-repo dual-context chat with history. |
| Deployments | `/api/v1/deployments` | Per-workspace HTTP deploy on a dedicated port; deactivate. |
| Agents | `/api/v1/agents` | Forge v2 plan/run/jobs, event-wait signalling, explicit `/route`. |
| Jobs | `/api/v1/jobs` | Job lifecycle. |
| Gateway | host-scoped | `*.thinksync.art` subdomains proxied to the deployed workspace runtime (see below). |
| WebSocket | `/v1/ws/jobs/{job_id}` | Live job event stream. |

See [`backend/ENDPOINTS.md`](backend/ENDPOINTS.md) for the full request/response reference.

### Gateway host boundary
Routing is decided **before** any router runs. Requests on a genuine workspace subdomain (`*.thinksync.art`) are dispatched straight to the Gateway proxy for their entire URL space, so user-app routes are never shadowed by platform routers. Platform hosts (`app.`, `api.`, apex) fall through to normal FastAPI routing. `gateway.router` is intentionally **not** registered as a global catch-all (that caused the `d016a14` regression).

### Trailing-slash parity
Platform collection roots (`/servers`, `/workspaces`, `/jobs`) accept both slash-less and slash forms by normalising the ASGI path scope at the edge — no `307` redirect (which previously leaked the rewrite Host and broke the browser). `redirect_slashes=False` on the app.

---

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js 20+
- A [Supabase](https://supabase.com) project (Postgres + RLS)
- A Google OAuth client ID (`GOOGLE_CLIENT_ID`)

### 1. Environment variables

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```env
# Supabase (Postgres only — Supabase Auth is disabled)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...   # NEVER expose to client

# JWT (custom, HS256)
JWT_SECRET=change-me-to-a-random-256-bit-secret
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=1440

# Google OAuth — the ONLY auth method
GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com

# Production safety: mandatory or the app refuses to start
APPROVAL_RESUME_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# Fernet key for SSH credential encryption at rest
DATA_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# LLM (OpenAI-compatible; SiliconFlow is the default base URL)
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini   # or a role-specific OPENAI_MODEL_PLANNER / _EXECUTOR / ...

# SSH security
SSH_STRICT_HOST_KEY_CHECKING=true
SSH_KNOWN_HOSTS=~/.ssh/known_hosts

# CORS (JSON array)
CORS_ORIGINS=["http://localhost:3000","https://app.thinksync.art"]
```

> Optional: `REDIS_URL` (Upstash) enables Forge v2 LLM response caching, job-event Pub/Sub, and chat memory.

### 2. Database setup

Apply the Supabase schema (in order) in the Supabase SQL editor:

```
infra/supabase/schema.sql
```

Startup diagnostics (`_run_startup_diagnostics` in `main.py`) verify the required tables (`job_steps`, `job_decisions`, `job_retries`, `job_execution_details`) and columns (`jobs.deleted_at`, `jobs.recoverable`, `jobs.recovery_reason`, `job_events.trace_id`) and log any gaps.

### 3. SSH known_hosts

Create the file before starting the backend:

```bash
mkdir -p ~/.ssh && touch ~/.ssh/known_hosts
chmod 700 ~/.ssh && chmod 600 ~/.ssh/known_hosts
```

New servers register automatically when added via the API.

### 4. Start locally

```bash
chmod +x scripts/start.sh
./scripts/start.sh
```

| Service | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| Backend | http://localhost:8000 |
| API docs | http://localhost:8000/docs *(only when `DEBUG=true`)* |

Production deployment helpers live under `scripts/` (`setup_prod.sh`, `deploy.sh`, `harden_ubuntu.sh`, `ssl_certbot_digitalocean.sh`) and `infra/docker-compose.yml`.

---

## Configuration

All settings are environment-driven via `core/config.py` (`pydantic-settings`). Highlights:

| Setting | Default | Purpose |
|---------|---------|---------|
| `DEBUG` | `false` | Enables `/docs`; tightens error surfacing. |
| `AGENT_ALLOW_WRITE` | `true` | **Global write kill-switch** for the whole pipeline. |
| `APPROVAL_RESUME_SECRET` | — | Mandatory in prod; required to resume suspended jobs. |
| `DECISION_ENGINE_MODE` | `off` | `off` \| `shadow` \| `weighted` \| `authoritative`. |
| `WAIT_TIMEOUT_SECONDS` | `1800` | Job suspend window, clamped to [1800, 3600]. |
| `AGENT_CONTEXT_MAX_FILES` / `_TOTAL_LINES` / `_LINES_PER_FILE` | `3` / `260` / `120` | Context budget. |
| `REDIS_URL` | `None` | Optional cache/events/memory backend. |
| `OPENAI_*` | — | Base + per-role model overrides (see `resolve_model()`). |
| `SSH_STRICT_HOST_KEY_CHECKING` | `true` | Enforce host-key verification in prod. |

---

## Testing

The backend has a large pytest suite under `backend/tests/` (247 tests collected). Run with the project venv:

```bash
cd backend
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

> **Note:** `backend/test_endpoints.py` imports `requests`, which is not in `requirements.txt`. Either `pip install requests` or exclude it (`pytest --ignore=test_endpoints.py`) until the dependency is added. Most service-layer tests run without a live database.

---

## Security

- `HTTPBearer(auto_error=False)` — missing/invalid tokens return `401`, not `403`.
- SSH strict host-key checking (configurable via `SSH_STRICT_HOST_KEY_CHECKING`).
- SSH credentials encrypted at rest (Fernet, `enc:v1:` prefix).
- UUID validation on all deletion endpoints.
- Row-level security (RLS) enabled on all Supabase tables.
- Sensitive fields redacted in HTTP request/response logs (`password`, `ssh_password`, `ssh_key`, `access_token`, `authorization`).
- Gateway host boundary isolates platform vs. workspace runtime traffic.
- Approval gate is the production kill-switch for write actions.

---

## Roadmap

- [x] Google OAuth (email/password removed)
- [x] SSH server management with connection validation
- [x] SSH command execution
- [x] Workspace + subdomain deployment
- [x] Workspace chat (dual context)
- [x] **Forge v2** — constitutional, platform-aware, self-healing, unified pipeline
- [x] Credential encryption at rest
- [x] Agent audit logging
- [x] Background worker + durable job queue
- [x] Event Wait Engine (suspend/resume without polling)
- [x] Decision Engine (shadow / weighted / authoritative)
- [x] WebSocket live job-event streaming
- [x] Context Engineering (Project Brain, repository index, progressive context)
- [ ] Server monitoring & alerting dashboard
- [ ] Pipeline / workflow builder
- [ ] Multi-user teams & role management

---

*For AI contributors: `THINKSYNC.md` is the persistent **Project Brain** (engineering memory) — read it before making changes, and append/patch rather than rewrite.*
