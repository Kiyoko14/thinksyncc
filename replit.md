# ThinkSync

Full-stack workspace platform: FastAPI backend + Next.js frontend + Redis-backed gateway.

## Architecture

- **Frontend**: Next.js 14, port 5000 (workflow: "Frontend")
- **Backend**: FastAPI + Uvicorn, port 8000 (workflow: "Backend API")
- **State**: Supabase (Postgres) + Redis (gateway routing, port allocation, rate limit, health)
- **Remote workspaces**: PM2 on managed servers via SSH

## Subdomain model

`{normalized_name}-{slug}.thinksync.art`

- `normalized_name`: `[a-z0-9]{1,10}` (auto-sanitized in `_sanitize_workspace_name`)
- `slug`: random `[a-z0-9]{6}`, globally unique in DB
- Total subdomain length capped at 63
- Reserved subdomains: `app`, `api`, `www` → gateway returns 404

## Gateway (`backend/routers/gateway.py`)

- Root catch-all `/{path:path}` registered last in `main.py` (existing API routes match first by FastAPI registration order)
- Strict regex check: `^[a-z0-9]{1,10}-[a-z0-9]{6}$`
- Per-workspace concurrency: `asyncio.Semaphore(10)` per workspace ID via `defaultdict`
- Sliding-window rate limit: Lua script over Redis sorted set `rate:sw:{ws}:{ip}` (100/60s)
- Pooled `httpx.AsyncClient` (max 200 conns, 50 keepalive) lifecycle-managed in `main.py` lifespan
- Path-based timeout: `/api*` → 15 s, else → 5 s
- Forwards `X-Request-ID` upstream; returns 502 on timeout/connect/error

## Redis keys (single source of truth)

- `ws:{workspace_id}:port` → int
- `ws_domain:{subdomain}` → workspace_id
- `ws:active` (set) → all currently deployed workspace IDs
- `ws:{workspace_id}:health` → "healthy" | "unhealthy" (TTL 300s)
- `ports:free` / `ports:used` → allocator pool (3000–8000)
- `rate:sw:{workspace_id}:{ip}` → sliding window sorted set

## Port allocator (`backend/services/port_allocator.py`)

- Atomic Lua script for `SPOP + SET + SADD`
- Range 3000–8000
- `allocate_port` is idempotent (returns existing port)
- `release_port` and `remove_from_active` keep `ws:active` consistent
- Startup consistency check + 30s background health-check loop

## Deployment flow (`backend/services/deployment_service.py`)

Strict order: PM2 start → `verify_http_server` exit 0 → Redis sync.
Redis sync block raises HTTP 500 ("Critical: Redis sync failed") on any failure.
Subdomain rebuilt from `name + slug` (no DB `domain` dependency).

## Health checker (`backend/services/health_checker.py`)

- Reads `ws:active` set (O(active) instead of SCAN)
- First probe detects `/health` vs `/`, then locks to detected endpoint (no fallback)
- 5 consecutive failures → eviction from `ws:active`

## Required secrets

- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`
- `JWT_SECRET`
- `REDIS_URL` (sync client works; async client expects standard `redis://` or `rediss://` — current value's async parse fails and degrades gracefully to no rate limit / no health loop)

## Known issues

- Async Redis client cannot parse the current `REDIS_URL` (Upstash REST-token format). Sync client works. Health checker and rate limiter degrade gracefully (logged warnings, no crashes).
