# ThinkSync

> AI-powered DevOps platform — connect servers, run intelligent agent workflows, deploy code securely.

---

## Overview

ThinkSync gives engineering teams a single control plane over their infrastructure. Add SSH servers, execute commands, deploy workspaces, and run **Forge v1** — a structured AI agent that plans and executes multi-step operations on your servers with full audit logging and async job tracking.

---

## Architecture

ThinkSync is a monorepo with a clean separation between modules:

```
thinksync/
├── backend/        FastAPI — Python 3.12
├── frontend/       Next.js 14 — TypeScript, Tailwind CSS
├── agents/         Agent definitions and extensions
├── infra/          nginx, docker-compose, Supabase schema
└── scripts/        Local dev & deployment helpers
```

### Backend (`backend/`)

Strict layered architecture — no cross-layer coupling:

| Layer | Responsibility |
|-------|----------------|
| `routers/` | HTTP parsing, response serialisation, route guards |
| `services/` | All business logic — never imported peer-to-peer |
| `models/` | Pydantic request / response schemas |
| `core/` | Config, security, database client, cryptography |

**Invariants:**
- Routers contain zero business logic — they delegate entirely to services.
- Services never import from routers.
- All configuration is sourced from environment variables via `core/config.py`.
- SSH credentials are encrypted at rest using Fernet symmetric encryption (`core/crypto.py`).

### Frontend (`frontend/`)

Next.js 14 App Router. All pages are client-side protected via a `localStorage` token check.

| Route | Description |
|-------|-------------|
| `/login` | Email + password login |
| `/dashboard` | Home screen after login |
| `/servers` | Add, list, and delete servers |

API calls are centralised in `services/api.ts` (typed fetch wrapper) and `services/auth.ts` (login / logout / token storage).

---

## Features

### Infrastructure
- [x] Email / password authentication with JWT (HS256)
- [x] Add, list, and delete SSH servers
- [x] SSH connection validation at server creation — fake / unreachable hosts are rejected immediately
- [x] Automatic `known_hosts` registration when a new server is added
- [x] SSH credentials encrypted at rest (Fernet, `enc:v1:` prefix)
- [x] SSH command execution — returns stdout, stderr, exit code, and execution time

### Workspaces & Deployments
- [x] Workspace creation with auto-generated slug and subdomain
- [x] HTTP deployment per workspace on a dedicated port (bound to `127.0.0.1`)
- [x] Workspace chat with persistent message history
- [x] One-click deployment deactivation

### Forge v1 Agent
- [x] **Instant plan** — keyword-based step generation with no SSH round-trip (<10 ms)
- [x] **Async run** — submit a job and receive `202 Accepted` + `job_id` immediately
- [x] **Job polling** — check status (`queued → running → completed / failed`) at any time
- [x] **Synchronous run** — blocking execution for use in scripts and pipelines
- [x] **Orchestration** — agent integrated with workspace chat history and architecture decision log
- [x] RBAC — write-capable commands restricted to admin emails
- [x] Environment allowlist — only pre-approved command prefixes execute on remote hosts
- [x] Parallel step execution with configurable concurrency (`AGENT_MAX_CONCURRENCY`)
- [x] Full audit log written to `agent_runs` table in Supabase
- [x] Per-step timeout enforcement (`AGENT_STEP_TIMEOUT`)

### Security
- [x] `HTTPBearer(auto_error=False)` — missing or invalid tokens return `401`, not `403`
- [x] SSH strict host key checking (configurable via `SSH_STRICT_HOST_KEY_CHECKING`)
- [x] UUID validation on all deletion endpoints
- [x] Row-level security (RLS) enabled on all Supabase tables

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | Python 3.12, FastAPI, asyncssh >= 2.14.2 |
| Database | Supabase (Postgres + PostgREST) |
| Auth | Supabase Auth + custom JWT (HS256) |
| Encryption | `cryptography` >= 42 — Fernet symmetric encryption |
| Proxy | nginx |
| Containers | Docker + Docker Compose |

---

## Getting Started

### Prerequisites

- Python 3.12+
- Node.js 20+
- A [Supabase](https://supabase.com) project

### 1. Environment variables

```bash
cp .env.example .env
```

Edit `.env` and set:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
JWT_SECRET=change-me-in-production

# SSH security
SSH_STRICT_HOST_KEY_CHECKING=true
SSH_KNOWN_HOSTS=~/.ssh/known_hosts

# Fernet key — generate with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DATA_ENCRYPTION_KEY=...

# Forge v1 agent
AGENT_ADMIN_EMAILS=admin@example.com
AGENT_STEP_TIMEOUT=45
AGENT_MAX_CONCURRENCY=2
AGENT_AUDIT_LOGGING_ENABLED=true
```

### 2. Database setup

Run the following SQL files in the Supabase SQL editor, in order:

```
infra/supabase/schema.sql
backend/db/migrations/20260322_subdomain_deployment.sql
backend/db/migrations/20260322_workspace_chat.sql
backend/db/migrations/20260404_agent_runs.sql
```

### 3. SSH known_hosts

Create the file before starting the backend:

```bash
mkdir -p ~/.ssh && touch ~/.ssh/known_hosts
chmod 700 ~/.ssh && chmod 600 ~/.ssh/known_hosts
```

New servers are registered automatically when added via the API.

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

### 5. Backend only

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env .env
uvicorn main:app --reload
```

### 6. Frontend only

```bash
cd frontend
npm install
npm run dev
```

---

## Deployment

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

Runs `docker compose build && docker compose up -d` from `infra/docker-compose.yml`.

| Domain | Service |
|--------|---------|
| `app.thinksync.art` | Next.js (port 3000) |
| `api.thinksync.art` | FastAPI (port 8000) |

> For HTTPS, configure Certbot / Let's Encrypt and update `infra/nginx/nginx.conf`.

---

## API Reference

Full interactive documentation: `GET /docs` (requires `DEBUG=true`).

### Health

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | — | Liveness check |

### Authentication

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/auth/login` | — | Login — returns JWT |
| `GET` | `/api/v1/auth/me` | Bearer | Current user info |
| `POST` | `/api/v1/auth/logout` | Bearer | Logout |

### Servers

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/servers/` | Bearer | List servers |
| `POST` | `/api/v1/servers/` | Bearer | Add server (validates SSH + registers known_hosts) |
| `DELETE` | `/api/v1/servers/{id}` | Bearer | Delete server |

### Commands

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/commands/execute` | Bearer | Execute SSH command on a server |

```json
{ "server_id": "uuid", "command": "uptime" }
```

### Workspaces

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/workspaces/` | Bearer | List workspaces |
| `POST` | `/api/v1/workspaces/` | Bearer | Create workspace |
| `DELETE` | `/api/v1/workspaces/{id}` | Bearer | Delete workspace |

### Deployments

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/deployments/{workspace_id}` | Bearer | Create / activate deployment |
| `GET` | `/api/v1/deployments/{workspace_id}` | Bearer | Get deployment status |
| `DELETE` | `/api/v1/deployments/{workspace_id}` | Bearer | Deactivate deployment |

### Chat

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/chat/{workspace_id}` | Bearer | Get or create workspace chat |
| `POST` | `/api/v1/chat/{workspace_id}/message` | Bearer | Send message |
| `POST` | `/api/v1/chat/message` | Bearer | Send message (workspace or git repo context) |

### Forge v1 Agent

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/agents/forge/plan` | Bearer | Generate a step-by-step plan instantly (no SSH) |
| `POST` | `/api/v1/agents/forge/run` | Bearer | Submit async job — returns `202` + `job_id` |
| `GET` | `/api/v1/agents/forge/jobs/{job_id}` | Bearer | Poll job status and result |
| `POST` | `/api/v1/agents/forge-v1/run` | Bearer | Synchronous full execution (blocking) |
| `POST` | `/api/v1/agents/forge-v1/orchestrate` | Bearer | Orchestration with chat + architecture history |

#### Async workflow example

```bash
# 1. Generate a plan (instant)
curl -X POST http://localhost:8000/api/v1/agents/forge/plan \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"server_id":"<uuid>","objective":"check disk and memory usage","max_steps":4}'

# 2. Submit the job
curl -X POST http://localhost:8000/api/v1/agents/forge/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"server_id":"<uuid>","objective":"check disk and memory usage","max_steps":4}'
# → 202 Accepted  { "job_id": "...", "status": "queued" }

# 3. Poll for result
curl http://localhost:8000/api/v1/agents/forge/jobs/<job_id> \
  -H "Authorization: Bearer $TOKEN"
# → { "status": "completed", "result": { ... } }
```

---

## Database Schema

- Core schema: [infra/supabase/schema.sql](infra/supabase/schema.sql)
- Subdomain / deployment tables: [backend/db/migrations/20260322_subdomain_deployment.sql](backend/db/migrations/20260322_subdomain_deployment.sql)
- Workspace chat tables: [backend/db/migrations/20260322_workspace_chat.sql](backend/db/migrations/20260322_workspace_chat.sql)
- Agent audit log: [backend/db/migrations/20260404_agent_runs.sql](backend/db/migrations/20260404_agent_runs.sql)

Row-level security (RLS) is enabled on all tables — users can only read and write their own data.

---

## Engineering Principles

1. **Modules stay small and focused.** One responsibility per file.
2. **No logic in routers.** Services own all business decisions.
3. **Async throughout.** `asyncssh`, async FastAPI routes, async job queue.
4. **Env vars everywhere.** No hardcoded values — everything via `core/config.py`.
5. **Security by default.** Strict SSH host checking, encrypted credentials, 401 on missing tokens, UUID-validated deletes.
6. **Fail fast at the boundary.** SSH connection validated before any DB write; rejected hosts never enter the system.

---

## Roadmap

- [x] Authentication and session management
- [x] SSH server management with connection validation
- [x] SSH command execution
- [x] Workspace and subdomain deployment
- [x] Workspace chat
- [x] **Forge v1 agent** — plan, async run, poll, orchestrate
- [x] Credential encryption at rest
- [x] Agent audit logging
- [ ] Real-time log streaming (WebSocket)
- [ ] Server monitoring and alerting dashboard
- [ ] Pipeline / workflow builder
- [ ] Multi-user teams and role management
- [ ] Redis-backed token deny-list for stateful logout
- [ ] Forge v2 — self-correcting agent with memory and tool use
