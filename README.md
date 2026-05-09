# ThinkSync

> AI-powered DevOps platform — connect servers, run intelligent agent workflows, deploy code securely.

---

## Overview

ThinkSync gives engineering teams a single control plane over their infrastructure. Add SSH servers, execute commands, deploy workspaces, and run **Forge v2** — a structured AI agent that plans and executes multi-step operations on your servers with full audit logging and async job tracking.

---

## Agent Constitution

To address challenges like objective drift, lack of platform awareness, and inconsistent success criteria, Forge v2 operates under a strict **Constitution**.

| Principle | Enforcement |
|---|---|
| **Objective Adherence** | The agent's actions are continuously checked against its initial objective to prevent drift. |
| **Platform Awareness** | The agent is required to load its platform context (ports, subdomains, SSL) before execution. Failure to do so results in a critical error. |
| **Success Contract** | Deployments are only marked as successful if they pass a rigorous, multi-stage verification process, including local, gateway, and public URL checks. |
| **Runtime Integrity** | The agent is prevented from redundant or contradictory actions, such as re-initializing a service that is already running. |
| **Tool Discipline** | Only a pre-approved set of tools can be used, preventing arbitrary command execution. |
| **Safety** | Dangerous commands (e.g., `rm -rf /`) are blocked unless explicitly confirmed by the user. |

This constitutional framework ensures that the agent behaves in a predictable, reliable, and secure manner.

---

## Architecture

ThinkSync is a monorepo with a clean separation between modules:

```
thinksync/
├── backend/        FastAPI — Python 3.12
├── frontend/       Next.js 14 — TypeScript, Tailwind CSS
├── agents/         Agent Constitution and core definitions
├── infra/          nginx, docker-compose, Supabase schema
└── scripts/        Local dev & deployment helpers
```

### Backend (`backend/`)

Strict layered architecture — no cross-layer coupling:

| Layer | Responsibility |
|-------|----------------|
| `routers/` | HTTP parsing, response serialisation, route guards |
| `services/` | All business logic — services include `agent_service`, `workspace_service`, `server_service`, etc. |
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

### Forge v2 Agent
- [x] **Constitutional AI** — Operates under a strict set of rules for safety, reliability, and objective adherence.
- [x] **Platform Aware** — Fails fast if it cannot determine its operating environment (port, subdomain, etc.).
- [x] **Rigorous Success Contract** — Multi-stage verification for deployments ensures services are actually live.
- [x] **Self-Healing** — Capable of analyzing failures and attempting corrective actions.
- [x] **Async Execution** — Jobs are queued and run in the background, allowing for non-blocking workflows.
- [x] **Full Audit Trail** — Every step, decision, and error is logged for complete transparency.
- [x] **Tool-Based** — Execution is based on a well-defined set of tools, not arbitrary shell commands.

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
| Agents | Custom LLM integration with Constitutional AI framework |
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

# Forge v2 agent
AGENT_STEP_TIMEOUT=45
AGENT_MAX_CONCURRENCY=2
```

### 2. Database setup

Run the following SQL files in the Supabase SQL editor, in order:

```
infra/supabase/schema.sql
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

---

## API Reference

Full interactive documentation: `GET /docs` (requires `DEBUG=true`). The API follows a standard RESTful structure for managing servers, workspaces, and agent jobs.

---

## Roadmap

- [x] Authentication and session management
- [x] SSH server management with connection validation
- [x] SSH command execution
- [x] Workspace and subdomain deployment
- [x] Workspace chat
- [x] **Forge v2 Agent** — Constitutional, platform-aware, and self-healing.
- [x] Credential encryption at rest
- [x] Agent audit logging
- [ ] Real-time log streaming (WebSocket)
- [ ] Server monitoring and alerting dashboard
- [ ] Pipeline / workflow builder
- [ ] Multi-user teams and role management
