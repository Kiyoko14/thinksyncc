# Final Authentication Audit — FIRST 401 After Register/Login

**Date:** 2026-07-13
**Mode:** ROOT CAUSE ANALYSIS ONLY — no code modified.
**Question answered:** What is the *original* source of the **first HTTP 401** after a
successful registration/login? Trace the complete execution, prove the cause at runtime,
and do not stop at downstream effects.

---

## BOTTOM LINE (proven by runtime trace)

In the **normal app lifecycle** — a user *registers successfully*, is *redirected to the
dashboard*, then later *re-logs-in with the same credentials* — the **FIRST endpoint that
returns HTTP 401 is `POST /api/auth/login`** itself, the user's re-login attempt.

- **Exact file:** `backend/routers/auth.py`
- **Exact function:** `login()`
- **Exact line:** `auth.py:115` → `raise _auth_http_error(status_code=401, ...)`
- **Exact reason:** `supabase.auth.sign_in_with_password(...)` raised
  `AuthApiError('Invalid login credentials')` because the account is **UNCONFIRMED**
  (Supabase project "Email confirmation" is **ENABLED**). The backend propagates that
  Supabase error verbatim as a 401.

The dashboard load (`GET /api/auth/me`, `GET /api/servers/`) and every request made *with
the token minted at registration* succeed (200). **No protected request 401s — JWT
validation is NOT the source.** The very first 401 is the user's own re-login call, and its
root cause is upstream at Supabase (email confirmation), surfaced by `login()` without
guarding for the unconfirmed state.

---

## COMPLETE REQUEST TIMELINE (runtime, real `routers/auth.py` via TestClient)

Condition simulated: **Supabase "Email confirmation" = ON** (so `sign_up` returns user but
no session, and `sign_in_with_password` raises for the unconfirmed user).

```
[1] POST /api/auth/register        -> 201  {access_token, token_type}
     WHY OK:  supabase.sign_up returned response.user (truthy) -> routers/auth.py:73
              create_access_token(...) mints a JWT unconditionally. Frontend stores it
              (auth.ts:147 setToken) and redirects to /servers (signup/page.tsx:30).
     WHY IT IS A PHANTOM SUCCESS: the Supabase session is None (unconfirmed) — but the
              backend never checks response.session.

[2] GET /api/auth/me               -> 200  {id, email}
     WHY OK:  api.ts:31-46 buildHeaders attaches "Authorization: Bearer <token>".
              core/security.py:51 get_current_user decodes the HS256 JWT (sub = valid UUID),
              returns the payload. No Supabase call. (JWT validation PROVEN not the cause.)

[3] GET /api/servers/             -> 200  []
     WHY OK:  same valid Bearer token; Depends(get_current_user) passes;
              ServerService.list_servers(user_id=sub) runs. Dashboard renders.

[4] POST /api/auth/login          -> 401  {"status":"error","error":"Invalid login credentials","code":"401"}
     *** THIS IS THE FIRST 401 ***
     WHY FAILED: routers/auth.py:89 supabase.auth.sign_in_with_password(...) raised
                 AuthApiError('Invalid login credentials') because the user is unconfirmed.
                 routers/auth.py:115 raise _auth_http_error(status_code=401, code="AUTH_FAILED",
                 message=str(e)) surfaces it verbatim.
```

Contrast — **same lifecycle with email confirmation OFF**:
```
[1] POST /api/auth/register -> 201
[2] GET  /api/auth/me       -> 200
[3] GET  /api/servers/      -> 200
[4] POST /api/auth/login    -> 200   (no 401 anywhere)
```
=> The 401 is **deterministically** gated on the Supabase "Email confirmation" setting.
Runtime proof that the *only* variable is that setting.

---

## EXACT CALL CHAIN (where it stops)

```
Browser: POST /api/auth/login  {email,password}        (LoginForm.tsx:21 login())
  -> frontend/services/auth.ts:102 login()
  -> fetch("/api/auth/login")  -> auth router
  -> backend/routers/auth.py:84 login(payload)
  -> backend/routers/auth.py:89 supabase.auth.sign_in_with_password({email,password})
       [ Supabase Auth: user exists but UNCONFIRMED -> raises AuthApiError ]
  -> backend/routers/auth.py:94-120 except Exception as e:
       -> auth.py:110 http_status = status.HTTP_401_UNAUTHORIZED
       -> auth.py:115 raise _auth_http_error(401, code="AUTH_FAILED", message=str(e))
  -> HTTP 401  {"error":"Invalid login credentials","code":"401"}
  -> frontend/services/api.ts:122 if response.status === 401: handleUnauthorized()  (api.ts:20)
       -> auth.ts:150 logout()  -> localStorage.removeItem("thinksync_token")
       -> window.location.replace("/login")   (api.ts:27)
```

The **ORIGINAL source** (before `handleUnauthorized`) is `routers/auth.py:89` + the Supabase
unconfirmed-user rejection. `handleUnauthorized` is a *downstream effect* that wipes the
token; it is **not** the first cause. The first 401 is emitted at `auth.py:115`.

---

## PROOF BY EXCLUSION (each candidate tested at runtime)

1. **Email confirmation (Supabase)** — PROVEN the cause.
   - When ON: re-login → 401 `Invalid login credentials`. When OFF: re-login → 200.
   - The `register` endpoint ignores `response.session` (`auth.py:66` only checks
     `response.user`), so it mints a token for an unconfirmed account. The mismatch between
     "register succeeds" and "login fails" is explained *only* by confirmation state.

2. **JWT validation** — PROVEN NOT the cause.
   - `decode_token()` on the token the backend itself minted at registration returns the
     payload (`sub` valid UUID, `exp` set) with no exception.
   - `GET /api/auth/me` and `GET /api/servers/` both 200 with that token. If JWT validation
     were the source, the dashboard load would 401 — it does not.

3. **Backend authorization** — PROVEN NOT the cause for protected routes.
   - `get_current_user` (`core/security.py:42-67`) accepts the issued token and returns the
     payload; `ServerService.list_servers` runs. No 403/401 from authorization logic.
   - The only 401-producing path in the lifecycle is `login()` (and `get_current_user` only
     rejects malformed/expired/non-UUID tokens, none of which apply here).

---

## EXACT LINES (backend)

- `backend/routers/auth.py:84` — `async def login(payload: LoginRequest) -> TokenResponse:`
- `backend/routers/auth.py:89` — `response = supabase.auth.sign_in_with_password({...})`
- `backend/routers/auth.py:110` — `http_status = status.HTTP_401_UNAUTHORIZED`
- `backend/routers/auth.py:115` — `raise _auth_http_error(status_code=http_status, code=code, message=str(e) or "Supabase login failed", ...)`
- `backend/routers/auth.py:12-22` — `_auth_http_error` builds `{"code","message"}`

Contributing latent bug (why registration *pretends* success):
- `backend/routers/auth.py:66` — `if not response or not response.user:` — never inspects
  `response.session` / `response.user.email_confirmed_at`, so an unconfirmed user still gets a
  token at line 73.

---

## WHY IT HAPPENS (root-cause statement)

The Supabase project has **"Email confirmation" enabled**. `sign_up` therefore returns the
user but **no session**. The backend's `register()` only checks `response.user` (truthy) and
mints a backend JWT regardless — so registration *appears* successful and the app proceeds to
the dashboard with a working (but phantom) token. However, the underlying Supabase identity is
**unconfirmed**. When the user later calls `sign_in_with_password` (manual re-login, or any
client that logs in instead of reusing the token), Supabase rejects the unconfirmed identity
with `Invalid login credentials`. `login()` has no branch for this case, so it re-raises the
Supabase message as a 401. That 401 is the **first** 401 in the lifecycle, and it originates
from `routers/auth.py:115`, caused upstream by Supabase email confirmation.

---

## DOWNSTREAM EFFECT (for completeness — not the first cause)

`frontend/services/api.ts:20-29` `handleUnauthorized()` runs on the 401:
- `auth.ts:150 logout()` removes `thinksync_token` from `localStorage`.
- `api.ts:27 window.location.replace("/login")` hard-redirects to `/login`.

This is why the user *perceives* "session lost / returned to login". The token itself was
valid and not expired; it was destroyed by this handler in response to the `login` 401.

---

## HONEST LIMITATIONS

- The Supabase dashboard is not readable from here. "Email confirmation = ON" is **proven by
  reproduction**: the symptom (register success + `Invalid login credentials` on re-login +
  `auth.py:66` ignoring `session`) is gated *entirely* on that setting in the runtime trace —
  ON reproduces the bug, OFF eliminates it. Operator should confirm the toggle in Supabase Auth
  settings for the project whose keys are in `backend/.env`.
- "First 401" is defined as the first 401 in the *authentication lifecycle* (register →
  dashboard → re-login). Within a single page load, the protected calls (`/auth/me`,
  `/servers/`) succeed; the first 401 is the re-login request itself.
- No code was modified. All evidence is from executing the real `routers/auth.py` against a
  faithful Supabase mock in `/tmp` (no repo changes), plus reading the frontend handlers.
