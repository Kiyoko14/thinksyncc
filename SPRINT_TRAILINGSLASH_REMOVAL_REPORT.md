# Root-Cause Analysis: `trailingSlash: true` Workaround Removal

**Date:** 2026-07-13
**Scope:** ThinkSync frontend (`next.config.js`) + backend (`main.py`) auth/API routing
**Verdict:** Root cause fixed in the backend; `trailingSlash: true` removed; platform routes now work identically with or without a trailing slash. No new redirects introduced.

---

## 1. Root cause

The platform routers register their **collection roots WITH a trailing slash**:

- `routers/servers.py:9,12,19` → prefix `"/servers"`, routes `"/"` → `"/servers/"`
- `routers/workspaces.py:12,24` → `"/workspaces/"`
- `routers/jobs.py:16,37` → `"/jobs/"`

When a browser calls the slash-less form (`GET /api/servers`), two redirects fire:

1. **Next.js** (App Router, `trailingSlash` default `false`) → `308` to `/api/servers`.
2. **FastAPI** (`redirect_slashes=True`) → `307` to `https://<rewrite-Host>/servers/`. The
   `Location` host is taken from the `Host` header of the Next.js rewrite, which is
   `INTERNAL_API_URL` (`http://backend:8000` in docker / `http://localhost:8000` in dev) — a host
   the **client browser cannot resolve** → browser `TypeError: Failed to fetch`.

The previous fix masked only step 1 with `next.config.js: trailingSlash: true`. It left
`redirect_slashes=True` in place (so the backend's 307 was still theoretically emitted for any
slash-less path that slipped past Next.js) and introduced the auth regressions described below.

## 2. Exact file / function / line

- **Backend fix:** `backend/main.py`
  - `app = FastAPI(..., redirect_slashes=False)` — `main.py:175` (changed 175-183).
  - New middleware `normalize_collection_root_slash(request, call_next)` — `main.py` (~line 268,
    registered right before `route_workspace_hosts_to_gateway`).
  - Module constant `_COLLECTION_ROOTS = frozenset({"/servers", "/workspaces", "/jobs"})`.
- **Frontend fix:** `frontend/next.config.js` — removed `trailingSlash: true` (was line 9).
- **Comment/cleanup:** `frontend/app/AuthBootstrap.tsx:9-10` (reworded comment; logic unchanged).

## 3. Why `trailingSlash` became necessary

It was a band-aid suppressing the Next.js `308` hop in the double-redirect chain so the backend's
`307` (to an unreachable `INTERNAL_API_URL` host) was never reached from the browser. It was never
a root fix — it only hid step 1 and created two new problems:

- **Auth regression #1 (navigation bounce):** `trailingSlash: true` made client-side navigation
  resolve public pages as `/signup/`, `/login/`. `AuthBootstrap.isPublicPath()` compared
  slash-less literals, so `/signup/` returned `false` → the guard called `router.replace("/login")`
  and Register bounced straight back to Login. (Mitigated at the time by slash-normalizing
  `isPublicPath`, but the dependency remained fragile.)
- **Auth regression #2:** any future `usePathname()`-based guard would misclassify slash-appended
  public routes.

## 4. Why it is no longer needed after the fix

The backend now accepts **both** `/servers` and `/servers/` with **NO redirect**:

- `redirect_slashes=False` removes FastAPI's `307`.
- `normalize_collection_root_slash` rewrites the *exact* collection roots (`/servers`,
  `/workspaces`, `/jobs`) to their canonical slash form **in the ASGI scope** (`request.scope["path"]`),
  so routing + the downstream handler see `/servers/` — no `Location` header is ever emitted.

Because no `307` is emitted, the next.config.js rewrite target (`INTERNAL_API_URL`) is never
followed by the browser, so it does not matter that the client cannot resolve it. `trailingSlash`
can stay at its Next.js default (`false`).

## 5. Minimal production-safe fix

1. `backend/main.py`: `FastAPI(redirect_slashes=False)` + add `normalize_collection_root_slash`
   middleware (only the three literal collection roots are normalized; exhaustive, no regex over
   arbitrary paths).
2. `frontend/next.config.js`: delete `trailingSlash: true`.
3. `frontend/app/AuthBootstrap.tsx`: keep the (now config-independent) slash normalization.

No router definitions, no API contracts, no auth flow, no frontend route, and no gateway boundary
were changed. Workspace subdomains still enter the Gateway untouched.

## 6. Verification performed (real output, not guesses)

**Backend unit (project venv) — `pytest tests/test_trailing_slash_parity.py tests/test_gateway_host_routing.py`:**
```
25 passed, 1 warning in 4.41s
```
Sample probe (real `main.app` via Starlette `TestClient`):
```
GET  /servers   -> 401  loc=None     GET  /servers/  -> 401  loc=None
POST /servers   -> 401  loc=None     GET  /workspaces-> 401  loc=None
GET  /jobs      -> 401  loc=None     POST /jobs      -> 401  loc=None
/auth/login     -> 400  loc=None      /health        -> 200  loc=None
ws GET /servers -> 299 (gateway)      platform POST /servers -> 401 (no gateway, no redirect)
```
Every collection root resolves for **both** slash forms with `loc=None` (no 3xx). Non-root
slash-less paths (`/chat`) correctly stay `404` (not silently rewritten). Workspace hosts still
enter the Gateway.

**Frontend unit (`node --test`):**
```
tests/test_auth_routing.js         -> 23 pass / 0 fail
tests/test_api_routing_no_failed_fetch.js -> 6 pass / 0 fail
```
The api-routing test now asserts `trailingSlash:true` is **absent** and that `/api/servers` and
`/api/servers/` do not redirect to `localhost`.

**Frontend production build:** `npm run build` → ✓ Compiled successfully, 9/9 routes, types valid.

**Live integration guard:** the `node --test` live probe to `https://app.thinksync.art/api/servers/`
and `/api/servers` returned no `localhost` redirect. NOTE: this hits the currently-deployed server
(which still runs the OLD code with `trailingSlash:true`); it passed because the deployed server
currently suppresses the 308. The unit/build tests above prove the NEW code is correct; deploy the
backend `main.py` change + `next build` to flip production to the root-cause fix.

## 7. Regression tests delivered

- `backend/tests/test_trailing_slash_parity.py` (NEW): asserts `redirect_slashes is False`,
  middleware registered, all collection roots parity (GET+POST, with/without slash) with NO
  redirect, non-collection paths untouched, workspace boundary intact.
- `backend/tests/test_gateway_host_routing.py`: updated `test_platform_no_slash_redirects_not_gateway`
  to assert NO 307 (previously asserted the buggy 307 — the test encoded the old broken behavior).
- `frontend/tests/test_api_routing_no_failed_fetch.js`: flipped to assert `trailingSlash:true`
  removed + both slash forms safe.
- `frontend/tests/test_auth_routing.js`: unchanged assertions (slash parity for Login/Register),
  comment updated to reflect backend-owned fix.

## 8. Remaining limitations (honest)

- The root-cause fix requires **both** artifacts deployed together: `backend/main.py` (redirect_slashes
  + middleware) AND `next build` for the frontend (so the removed `trailingSlash` takes effect). Until
  both ship, production still relies on the old `trailingSlash:true` (harmless but superseded).
- Live integration test depends on the deployed server; it passed against the old deployment and is
  retained as a guard for the new deployment.
- `INTERNAL_API_URL=http://backend:8000` (docker) is server-side only — correct and never followed by
  the browser. No change required; documented so a future operator does not "fix" it to a public host
  unnecessarily.
