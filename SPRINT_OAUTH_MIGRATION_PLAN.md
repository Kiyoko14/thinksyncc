# Google OAuth Migration — Audit & Migration Plan

**Goal:** Replace Supabase Auth (email/password) with Google OAuth as the only auth method.
Supabase stays as PostgreSQL only. Backend owns all identities. JWT format + protected-API
architecture + authorization middleware stay unchanged.

**Approach decision (from user):** Custom ThinkSync identity model.
- Create `public.users` as the canonical identity table.
- JWT `sub` becomes `public.users.id`.
- Store `google_sub` as a unique column.
- Migrate all FKs from `auth.users` → `public.users`.
- Future OAuth providers (GitHub, Microsoft, Telegram, Apple) reuse the same table.
- No dependency on `auth.users` remains.

This document is **PHASE 0 plan only** — audit + migration design. No DB or code is changed
until approved.

---

## 1. Architectural facts established by the audit

| # | Fact | Evidence |
|---|------|----------|
| F1 | `auth.users` is referenced as an FK target by **6 tables** | `backend/db/schema.sql` lines 9, 23, 36, 53, 61, 183 |
| F2 | **15 RLS policies** use `auth.uid()` for tenant isolation | `schema.sql` lines 269, 276, 283, 295, 311, 318, 325, 336, 355, 378, 399, 420, 441, 471, 492 |
| F3 | Backend connects as **service-role** (`core/database.py:17`) → **RLS is bypassed** | `core/database.py` |
| F4 | Tenant isolation is **app-enforced** via `.eq("user_id", user_id)` in services | `server_service.py:116,131,214`, `chat_service.py:131,147,315`, `agent_service.py:2525,2542`, `workspace_service.py:285,436,541,573,607` |
| F5 | JWT `sub` must be a UUID, required by `get_current_user` + all FKs | `core/security.py:59-65` |
| F6 | `public.users` does **NOT** exist yet | grep: 0 matches for `create table public.users` |
| F7 | Only `routers/auth.py` calls `supabase.auth.*` (register/login) | grep across all `.py` |
| F8 | Frontend stores opaque JWT (`thinksync_token`); reads `/auth/me` → `{id,email}` | `frontend/services/auth.ts`, `api.ts` |
| F9 | Frontend never reads `user_id` from any API or the JWT `sub` | grep `user_id|/auth/me|.sub|userId` in `.tsx` → 0 real matches |
| F10 | `PyJWT 2.13.0` available; `google-auth` NOT installed | venv check |

**Consequence:** Because of F3+F4+F9, migrating `auth.users`→`public.users` and changing
`sub`→`public.users.id` is **transparent to the frontend** and does **not** alter any
backend authorization logic (the `.eq("user_id", …)` filters keep working as long as the
IDs are preserved 1:1 in the data copy).

---

## 2. Every table and foreign key affected

Tables with `user_id uuid references auth.users(id)`:

| Table | FK column | Current target | New target |
|-------|-----------|----------------|------------|
| `servers` | `user_id` | `auth.users(id)` | `public.users(id)` |
| `workspaces` | `user_id` | `auth.users(id)` | `public.users(id)` |
| `chats` | `user_id` | `auth.users(id)` | `public.users(id)` |
| `chat_messages` | `user_id` | `auth.users(id)` | `public.users(id)` |
| `jobs` | `user_id` | `auth.users(id)` | `public.users(id)` |
| `tasks` | `user_id` | `auth.users(id)` | `public.users(id)` |

`messages` has **no** `user_id` (owned via `chat_id`→`chats`) → **not affected**.

RLS policies referencing `auth.uid()` (F2) — to be rewritten or dropped (see §6).

---

## 3. Every backend file that depends on `auth.users` / Supabase Auth

| File | Current dependency | Phase | Action |
|------|--------------------|-------|--------|
| `routers/auth.py` | `supabase.auth.sign_up` (register), `supabase.auth.sign_in_with_password` (login) | 1 → add `/auth/google`; 3 → remove register/login | Add Google endpoint; delete email/password endpoints in Phase 3 |
| `models/user.py` | `LoginRequest`, `RegisterRequest` (email/password) | 3 | Remove password models; keep `UserResponse`, `TokenResponse` |
| `core/security.py` | `get_current_user` requires UUID `sub` | — | **UNCHANGED** (sub stays UUID) |
| `core/database.py` | `get_supabase()` service-role client | — | **UNCHANGED** (Postgres access) |
| `core/config.py` | — | 1 | **ADD** `GOOGLE_CLIENT_ID`, cert URL const |
| `tests/test_auth_flow.py` | mocks `supabase.auth` register/login | 3 | **REPLACE** with Google OAuth flow tests |
| (all service-layer files) | filter by `user_id` | — | **UNCHANGED** (F4) |

---

## 4. Every frontend assumption about user IDs

| File | Assumption | Impact |
|------|-----------|--------|
| `services/auth.ts` | `login(email,pw)`→`/auth/login`; `register(email,pw)`→`/auth/register`; stores `thinksync_token`; reads `/auth/me`→email | Phase 2: replace with `loginWithGoogle(id_token)`; keep `setToken`/`getToken`/`logout` |
| `services/api.ts` | attaches `Bearer` token; 401→logout+`/login` | **UNCHANGED** (token handling identical) |
| `app/login/page.tsx` + `LoginForm.tsx` | email/password form | Phase 2: replace with "Continue with Google" |
| `app/signup/page.tsx` | register form | Phase 2: **DELETE** |
| `AuthBootstrap.tsx` | public path `/signup` | Phase 2: remove `/signup` from public list |
| `LoginForm.tsx` | "Register" link → `/signup` | Phase 2: remove |
| `Navbar.tsx`, `dashboard`, `servers`, `chat/*`, `page.tsx`, `BottomNav` | only `getToken()`/`logout()` + redirect to `/login` on missing token | **UNCHANGED** (no user_id assumption) |
| `frontend/tests/*` | (none) | no auth tests exist |

**Bottom line:** No frontend code depends on `auth.users` or the numeric/format of `user_id`.
Migrating `sub`→`public.users.id` needs no frontend change beyond the auth flow swap.

---

## 5. Proposed migration SQL (Phase 0 — additive, data-preserving)

Core DDL (new `public.users`, provider-agnostic for future OAuth):

```sql
create table if not exists public.users (
    id            uuid primary key default gen_random_uuid(),
    email         text not null,
    google_sub    text unique,
    display_name  text,
    avatar_url    text,
    provider      text not null default 'google',
    is_active     boolean not null default true,
    last_login_at timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create unique index if not exists idx_users_email        on public.users (email);
create unique index if not exists idx_users_google_sub   on public.users (google_sub) where google_sub is not null;
```

Data copy (preserve `auth.users.id` 1:1 so FKs stay valid):

```sql
insert into public.users (id, email, provider, created_at, last_login_at)
select id, coalesce(email, 'unknown-'||id||'@migrated.local'), 'google', created_at, last_sign_in_at
from auth.users
where email is not null
on conflict (id) do nothing;
```

FK remap (drop auto-named FK, recreate against `public.users`). Names follow Postgres
default `<table>_<column>_fkey`; **verify against live DB with `\d <table>`** before running:

```sql
alter table public.servers       drop constraint if exists servers_user_id_fkey;
alter table public.servers       add constraint servers_user_id_fkey
    foreign key (user_id) references public.users(id) on delete cascade;
-- repeat for workspaces, chats, chat_messages, jobs, tasks
```

RLS policies: see §6.

Also update `backend/db/schema.sql` (canonical) to reflect `public.users` + remapped FKs, and
regenerate the stale `infra/supabase/schema.sql` copy (per schema-audit methodology).

---

## 6. RLS decision (OPEN — needs your call)

Today RLS is bypassed by service-role (F3) and isolation is app-enforced (F4). The
`auth.uid()` policies are broken/inert once `auth.users` is gone. Three options:

- **(A) Drop the `auth.uid()` policies entirely.** Simplest. Functionally identical to
  today (service-role bypasses RLS anyway; app layer enforces ownership). Removes the
  `auth.users` dependency cleanly. *Recommended.*
- **(B) Keep RLS, switch to `current_setting('app.user_id')::uuid = user_id`.** Adds
  defense-in-depth at the DB layer, but requires the DB access layer to `SET LOCAL
  app.user_id` per request — a cross-cutting change to `core/database.py` + every query
  path. More invasive; not required by the brief.
- **(C) Defer RLS:** disable RLS on the 6 tables now, add (B) later as a separate hardening
  sprint.

---

## 7. Google OAuth implementation (Phase 1)

- **Config:** add `GOOGLE_CLIENT_ID: str`, `GOOGLE_ISSUERS` (default
  `https://accounts.google.com`, `https://securetoken.google.com/<project>`),
  `GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"`.
- **Verification (no `google-auth` dep):** `services/google_auth.py`:
  `verify_google_id_token(id_token, client_id) -> dict` — fetch + **cache** Google's public
  certs (PyJWT `jwt.decode(..., audience=client_id, issuer=..., options={...})`). In the
  sandbox, the cert fetch is **mocked** in tests (no network).
- **Upsert user:** by `google_sub`; on first login `INSERT` (email, display_name,
  avatar_url, last_login_at); on return `UPDATE last_login_at`.
- **New pydantic:** `GoogleLoginRequest { id_token: str }`; response reuses `TokenResponse`.
- **Endpoint:** `POST /auth/google` → verify → upsert → `create_access_token(subject=
  public.users.id, extra_data={"email": ...})` → `TokenResponse`.
- **Unchanged:** `/auth/me`, `/auth/logout`, `get_current_user`, JWT format.

---

## 8. Phase sequencing (proposed)

- **Phase 0 — Identity model (prerequisite):** `public.users` + FK remap + data copy + RLS
  fix + `schema.sql` sync. (This plan, pending approval.)
- **Phase 1 — Google OAuth login (backend):** `/auth/google`, config, `google_auth.py`,
  tests; Login→Dashboard verified via mocked Google + mocked supabase. Register/login kept
  (removed in Phase 3).
- **Phase 2 — Frontend:** "Continue with Google" button; delete Register page + form;
  remove `/signup` public path. No user_id change.
- **Phase 3 — Backend cleanup:** delete `/auth/register`, `/auth/login`, `LoginRequest`,
  `RegisterRequest`, dead Supabase-auth imports; replace `test_auth_flow.py` with Google
  OAuth regression tests; final RLS removal (per §6 choice).

---

## 9. Open items requiring approval

1. **Approve Phase 0 migration plan** (esp. the data-copy + FK-remap SQL).
2. **RLS strategy:** A / B / C (§6).
3. Confirm `GOOGLE_CLIENT_ID` will be supplied via env (no repo secret committed).

**No DB or code is modified until you approve.**
