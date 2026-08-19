# Root-Cause Analysis: Authentication / Session Bug

**Date:** 2026-07-13
**Mode:** ROOT CAUSE ANALYSIS ONLY — no code modified.
**Scope:** auth/session lifecycle only (routing & trailingSlash excluded, per instructions).

---

## TL;DR (proven)

There is **no Supabase session, no refresh token, no `AuthProvider`, no `onAuthStateChange`**
in this app. Authentication is a **backend-issued HS256 JWT** (24h) stored in
`localStorage`. The bug is:

> **Supabase project has "Email confirmation" ENABLEDI.** On `sign_up`, Supabase returns
> `user` (truthy) but `session = None`. The backend's `register` endpoint **ignores the
> missing session** and mints a backend JWT anyway → registration *appears* successful and
> the app redirects to the dashboard. But the user is **unconfirmed**, so when they later
> call `sign_in_with_password` with the same credentials, Supabase raises
> **`Invalid login credentials`** (HTTP 400). The backend surfaces that verbatim as a 401.
> Hence: register "succeeds", re-login with the same credentials fails, flow looks
> inconsistent.

Reproduced end-to-end against the real `routers/auth.py` with a faithful Supabase mock
(see "Runtime trace" below). **Register → 201 + access_token issued; immediate Login →
401 `Invalid login credentials`.** Toggling email confirmation OFF makes Login → 200.

---

## Exact architecture (what the code actually does)

| Layer | File | Behavior |
|---|---|---|
| Signup page | `frontend/app/signup/page.tsx:29-30` | `await register(email,password)` then `router.push("/servers")` |
| Register API | `frontend/services/auth.ts:126-148` `register()` | `POST /api/auth/register`; on `access_token` present → `setToken(token)` (localStorage) |
| Backend register | `backend/routers/auth.py:28-78` `register()` | `supabase.auth.sign_up(...)`; if `response.user` → `create_access_token(...)`; returns `TokenResponse(access_token)` |
| Backend login | `backend/routers/auth.py:84-134` `login()` | `supabase.auth.sign_in_with_password(...)`; on failure raises `_auth_http_error(401, "AUTH_FAILED", str(e))` |
| Token create | `backend/core/security.py:14-23` `create_access_token()` | HS256 JWT, `exp = now + JWT_EXPIRE_MINUTES` (24h, `config.py:24`) |
| Token store | `frontend/services/auth.ts:97-100` `setToken()` | `localStorage.setItem("thinksync_token", token)` |
| Token read | `frontend/services/auth.ts:155-171` `validateStoredToken()`/`getToken()` | reads localStorage, drops if expired |
| API auth header | `frontend/services/api.ts:31-46` `buildHeaders()` | `Authorization: Bearer <token>` |
| Unauthorized | `frontend/services/api.ts:20-29` `handleUnauthorized()` | on **any 401** → `logout()` + `window.location.replace("/login")` |
| Route guard | `frontend/app/AuthBootstrap.tsx:25-35` | if no token & not public → `router.replace("/login")` |
| Backend verify | `backend/core/security.py:42-67` `get_current_user()` | decodes JWT, requires `sub` be a valid UUID; raises 401 otherwise |
| Supabase client | `backend/core/database.py:8-18` `get_supabase()` | singleton using **service-role key** (bypasses RLS) |

**There is no `/auth/refresh` endpoint** (grep of `routers/`, `main.py` → only `/auth/register`,
`/auth/login`, `/auth/me`, `/auth/logout`). There is **no refresh token** anywhere. Frontend
never refreshes; the JWT simply expires after 24h.

---

## Runtime trace (real code, mocked Supabase — no repo change)

```
SCENARIO A — Supabase "Email confirmation" ENABLED
  [1] REGISTER  POST /auth/register  -> 201  {access_token, token_type}
        >>> backend issued a token even though Supabase returned session=None (unconfirmed)
  [2] LOGIN     POST /auth/login     -> 401  {"error":"Invalid login credentials","code":"401"}
        >>> Supabase rejects the unconfirmed user

SCENARIO B — "Email confirmation" DISABLED
  [1] REGISTER  -> 201  {access_token}   (user + session returned)
  [2] LOGIN     -> 200  {access_token}   (works)

SCENARIO C — duplicate register while confirmed
        Supabase returns 422 / "User already registered" -> backend 400 (correct failure)
```

The single decisive fact: in Scenario A, `register` **mints a token without a Supabase
session**. `routers/auth.py` only guards `if not response or not response.user` (line 66),
never inspecting `response.session` or `response.user.email_confirmed_at`.

---

## Answers to the 10 audit questions

1. **Is registration actually successful?** Partially. Backend returns 201 + JWT (so the app
   proceeds), but at Supabase the user is **created-but-unconfirmed** with **no session**.
2. **Is the user immediately logged in after signup?** App-level yes (a token is stored and
   `/servers` loads), but it is a *phantom session* — there is no Supabase session and the user
   is unconfirmed.
3. **Is the session being lost?** The `localStorage` token is **not** lost (valid 24h). The
   *perception* of "session lost" comes from the failed re-login and from `handleUnauthorized()`
   wiping the token on any 401 (see Q7).
4. **Is the access token disappearing?** No. It persists in `localStorage` until expiry.
5. **Is the refresh token failing?** N/A — **there is no refresh token and no refresh endpoint.**
   The frontend never refreshes; after 24h every call 401s and bounces to `/login`.
6. **Is Supabase rejecting the session?** Yes — specifically on **login**: `sign_in_with_password`
   raises `Invalid login credentials` for the unconfirmed user. (Register is accepted.)
7. **Is AuthBootstrap clearing auth state?** `AuthBootstrap` does **not** clear state — it only
   *redirects* when `validateStoredToken()` returns null. The actual clearer is
   `frontend/services/api.ts:20-29` `handleUnauthorized()`, which runs on **every** 401 and calls
   `logout()` (removes the token) + hard-redirects to `/login`. So any 401 silently destroys the
   stored token.
8. **Is there a race condition?** Minor. `AuthBootstrap` (`layout.tsx`) and each page's own
   `useEffect` (`servers/page.tsx:51-58`, `dashboard/page.tsx:12-18`, etc.) independently check
   the token and can both redirect. Not the primary bug, but redundant and racy.
9. **Is there duplicate auth state management?** Yes — three independent checks all read the same
   `localStorage` key (`AuthBootstrap`, per-page `getToken()`, `api.ts` `handleUnauthorized`).
   They don't diverge in value, but the triple-redundancy is fragile (e.g. one path can wipe the
   token another just validated).
10. **Is backend rejecting valid JWTs?** No. `get_current_user` accepts the issued JWT (the `sub`
    is a valid UUID). The 401 that causes the bounce originates from **Supabase login**, not from
    JWT verification.

---

## Root cause (exact)

- **File:** `backend/routers/auth.py`
- **Function:** `register()`
- **Lines:** `66-78` (guard + token mint)
  ```python
  66  if not response or not response.user:
  67      raise HTTPException(400, "Registration failed: user not returned (email confirmation may be required)")
  ...
  73  token = create_access_token(subject=str(response.user.id), extra_data={"email": response.user.email})
  78  return TokenResponse(access_token=token)
  ```
- **Why it happens:** When Supabase "Email confirmation" is **ON**, `sign_up` returns
  `response.user` (truthy) but `response.session = None`. The guard at line 66 passes, and the
  backend mints a JWT for a user that Supabase will not authenticate until confirmed. The login
  endpoint, by contrast, faithfully surfaces Supabase's `Invalid login credentials`.
- **Condition that triggers the symptom:** Supabase Auth → Providers → Email → **"Confirm email"**
  is enabled. (This is a Supabase *dashboard* setting, not in the repo, so it must be verified by
  the operator in the Supabase console for the project whose keys are in `backend/.env`.)
- **Secondary contributor:** `frontend/services/api.ts:20-29` `handleUnauthorized()` wipes the
  token on any 401, so once a single protected call fails the app hard-resets to `/login`.

---

## Why the "register success / login fail" inconsistency exists

- **Confirmed OFF:** `sign_up` → `{user, session}`; `register` mints token; `sign_in_with_password`
  succeeds. Consistent.
- **Confirmed ON:** `sign_up` → `{user, session=None}`; `register` **still** mints a token (silent
  success) and redirects to the dashboard; `sign_in_with_password` raises `Invalid login
  credentials` because the user is unconfirmed. Inconsistent — exactly the reported behavior.

---

## Minimal production-safe fix (DESCRIBED — not applied, analysis-only)

**Option 1 (recommended, dependency-free, keeps custom-JWT design):** make `register` honest.
- In `backend/routers/auth.py:register()`, after `sign_up`, check `response.session` (and/or
  `response.user.email_confirmed_at`). If there is **no session** (confirmation pending), return a
  clear, non-token response (e.g. `202 Accepted` with `{"detail": "Confirmation email sent. Please
  confirm your email before logging in."}`) and **do not** mint or return `access_token`. The
  frontend should then show "Check your email" instead of redirecting to `/servers`.
- This stops the phantom session and makes login-after-register consistent. After the user clicks
  the confirmation link, `sign_in_with_password` works normally.
- Also, in `login()`, map Supabase's `invalid_grant` / unconfirmed case to a friendlier message
  ("Please confirm your email before logging in") rather than a raw `Invalid login credentials`.

**Option 2 (if email confirmation is not wanted at all):** disable "Confirm email" in the Supabase
dashboard for this project. Then `sign_up` returns a session, `register` mints a token, and login
works immediately — no code change needed. (Choose this only if email verification is genuinely
unwanted.)

**Hardening (recommended regardless):**
- `frontend/services/api.ts:20-29`: `handleUnauthorized()` should not silently `logout()` on a
  *transient* 401 for a token that is still valid; only clear on genuine auth errors, and avoid the
  hard `window.location.replace` during in-flight navigations (it can clobber a just-set token).

---

## Regression tests to add (not yet written — analysis-only)

- `backend/tests/test_register_email_confirmation.py`:
  - confirm-ON mock → register returns **no** `access_token` (token mint blocked); HTTP 202/400
    with "confirmation" message.
  - confirm-OFF mock → register returns 201 + `access_token`; immediate login returns 200.
  - duplicate register → 400 with Supabase's existing-user error.
- `frontend/tests/`: register-with-pending-confirmation shows "check email" and does NOT navigate
  to `/servers`; login-with-unconfirmed shows the friendly message (not raw Supabase error).
- JWT verification: a token minted for an unconfirmed-but-registered user must still validate at
  `/auth/me` (proves backend doesn't reject its own JWTs) — to guard Q10.

---

## Honest limitations of this analysis

- I cannot read the Supabase dashboard, so "Email confirmation = ON" is **inferred** from the
  deterministic signature of the reproduced behavior (register success + `Invalid login credentials`
  on login + backend code at `auth.py:66` that ignores `session`). The operator should confirm the
  toggle in Supabase Auth settings. If the toggle is OFF, then the symptom would instead point to a
  Supabase-side credential/duplicate-user error, which the same `login()` path would surface — but
  the *register-issues-token-without-session* gap remains a latent bug either way.
- `handleUnauthorized()` wiping on any 401 is proven by reading `api.ts:20-29` (no runtime needed).
- No code was modified during this analysis.
