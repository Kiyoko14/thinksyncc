# ThinkSync v2

> AI DevOps Platform — connect servers, run commands, deploy code.

---

## Architecture

ThinkSync is a monorepo with a clean separation between modules:

```
thinksync/
├── backend/        FastAPI — Python 3.12
├── frontend/       Next.js 14 — TypeScript, Tailwind
├── agents/         Future AI agents
├── infra/          nginx, docker-compose, Supabase schema
└── scripts/        Local dev & deployment helpers
```

### Backend (`backend/`)

Follows a strict layered architecture:

| Layer | Responsibility |
|-------|----------------|
| `routers/` | HTTP request parsing, response serialisation, route guards |
| `services/` | All business logic — never imported by other services |
| `models/` | Pydantic request/response schemas |
| `core/` | Configuration, security, database client |

**Rules enforced:**
- Routers never contain logic — they delegate to services.
- Services never import from routers.
- All configuration comes from environment variables via `core/config.py`.

### Frontend (`frontend/`)

Next.js 14 App Router. All pages are client-side protected via `localStorage` token check.

| Path | Description |
|------|-------------|
| `/login` | Email + password login form |
| `/dashboard` | Home screen after login |
| `/servers` | Add / list / delete servers |

API communication is centralised in `services/api.ts` (typed Fetch wrapper) and `services/auth.ts` (login / logout / token storage).

---

## MVP Features (v1)

- [x] Authentication — login, session, logout
- [x] Servers — add, list, delete
- [x] SSH command execution — run a command and return stdout/stderr
- [x] Health check — `GET /health`

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js 14, TypeScript, Tailwind CSS |
| Backend | Python 3.12, FastAPI, asyncssh |
| Database | Supabase (Postgres + Auth) |
| Auth | Supabase Auth + custom JWT (HS256) |
| Proxy | nginx |
| Containers | Docker + Docker Compose |

---

## Getting Started

### Prerequisites

- Python 3.12+
- Node.js 20+
- A [Supabase](https://supabase.com) project

### 1. Set up environment

```bash
cp .env.example .env
# Edit .env and fill in SUPABASE_URL, SUPABASE_ANON_KEY,
# SUPABASE_SERVICE_ROLE_KEY, and JWT_SECRET
```

### 2. Set up the database

Run [infra/supabase/schema.sql](infra/supabase/schema.sql) in the Supabase SQL editor.

### 3. Start locally

```bash
chmod +x scripts/start.sh
./scripts/start.sh
```

| Service | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| Backend | http://localhost:8000 |
| API docs (dev) | http://localhost:8000/docs |

> Docs are only available when `DEBUG=true`.

### 4. Backend only

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env .env          # pydantic-settings reads .env from the working directory
uvicorn main:app --reload
```

### 5. Frontend only

```bash
cd frontend
npm install
npm run dev
```

---

## Deployment

### Docker Compose

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

This runs `docker compose build` and `up -d` from `infra/docker-compose.yml`.

Domains served by nginx:

| Domain | Service |
|--------|---------|
| `app.thinksync.art` | Next.js (port 3000) |
| `api.thinksync.art` | FastAPI (port 8000) |

> For HTTPS, add a Certbot / Let's Encrypt step and update `nginx.conf`.

---

## API Reference

### Authentication

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/auth/login` | Login — returns JWT |
| `GET` | `/api/v1/auth/me` | Current user |
| `POST` | `/api/v1/auth/logout` | Logout (client-side) |

### Servers

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/servers/` | List servers |
| `POST` | `/api/v1/servers/` | Add server |
| `DELETE` | `/api/v1/servers/{id}` | Delete server |

### Commands

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/commands/execute` | Run SSH command |

**Body:**
```json
{ "server_id": "uuid", "command": "uptime" }
```

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health check |

---

## Database Schema

See [infra/supabase/schema.sql](infra/supabase/schema.sql).

Row-level security (RLS) is enabled on all tables — users can only access their own data.

---

## Engineering Principles

1. **Modules stay small and focused.** One responsibility per file.
2. **No logic in routers.** Services own all business decisions.
3. **Async throughout.** asyncssh, async FastAPI routes.
4. **Env vars everywhere.** No hardcoded values — use `.env`.
5. **Extensible foundation.** The structure is ready for pipelines, AI agents, monitoring, and log streaming without requiring a rewrite.

---

## Roadmap (post-MVP)

- [ ] Pipeline builder
- [ ] AI diagnostic agent
- [ ] Real-time log streaming (WebSocket)
- [ ] Server monitoring & alerting
- [ ] Multi-user teams
- [ ] Redis-backed token deny-list for logout
