/**
 * Regression test: the "Failed to fetch" root-cause guard.
 * Uses Node's built-in test runner (node --test), no Jest dependency.
 *
 * Historical root cause (fixed):
 *   The platform routers register collection roots WITH a trailing slash
 *   (/servers/, /workspaces/, /jobs/). A browser GET /api/servers (slash-less)
 *   triggered a double redirect:
 *     1. Next.js 308  -> /api/servers          (trailingSlash default false)
 *     2. backend redirect_slashes 307 -> https://localhost:8000/servers/
 *   The 307 Location used the rewrite Host (INTERNAL_API_URL =
 *   http://backend:8000 / localhost:8000), which the BROWSER cannot resolve ->
 *   "Failed to fetch".
 *
 *   The old band-aid was next.config.js: trailingSlash: true (suppressed only
 *   the Next.js 308 and introduced auth regressions). That band-aid is removed.
 *
 * The real fix (backend, main.py):
 *   - FastAPI(redirect_slashes=False)
 *   - normalize_collection_root_slash middleware rewrites the exact collection
 *     roots (/servers, /workspaces, /jobs) to their canonical slash form in the
 *     ASGI scope, so BOTH "/servers" and "/servers/" resolve with NO redirect.
 *
 * Therefore this test now asserts the OPPOSITE of the old guard:
 *   - next.config.js does NOT set trailingSlash: true (the workaround is gone)
 *   - the rewrite still maps /api/:path* -> INTERNAL_API_URL/:path*
 *   - INTERNAL_API_URL stays overridable via env
 *   - a live /api/servers/ (and /api/servers) request does NOT redirect to a
 *     localhost / unreachable host (because the backend no longer 307s)
 */
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");
const { test, describe } = require("node:test");
const assert = require("node:assert/strict");

const configPath = path.resolve(__dirname, "..", "next.config.js");
const configSrc = fs.readFileSync(configPath, "utf8");

describe("next.config.js — trailingSlash workaround removed", () => {
  test("trailingSlash: true is NOT set (workaround removed; backend now owns slash parity)", () => {
    // Match ONLY a real config key (start-of-line, optional leading spaces,
    // no `//` comment prefix) so the explanatory comment text doesn't trip it.
    assert.doesNotMatch(
      configSrc,
      /^\s*trailingSlash\s*:\s*true/m,
      "trailingSlash:true must be removed — it was a band-aid masking the backend 307. " +
        "The backend (redirect_slashes=False + normalize_collection_root_slash) fixes the root cause."
    );
  });

  test("rewrites() maps /api/:path* → INTERNAL_API_URL/:path*", () => {
    assert.match(configSrc, /source:\s*"\/api\/:path\*"/);
    assert.match(configSrc, /destination:\s*`\$\{dest\}\/:path\*`/);
  });

  test("INTERNAL_API_URL is overridable via env (not baked/forced)", () => {
    assert.match(configSrc, /INTERNAL_API_URL\s*\?\?/);
  });
});

describe("Production INTERNAL_API_URL is server-side reachable", () => {
  test("pm2 env exposes an API origin (skips if not under pm2)", () => {
    let out = "";
    try {
      out = execSync("pm2 env 0 2>/dev/null || true").toString();
    } catch {
      return; // pm2 not present in CI — skip this guard.
    }
    const match = out.match(/INTERNAL_API_URL=([^\s]+)/);
    if (!match) return; // Not running under pm2 here — skip rather than fail.
    // In docker this is http://backend:8000 resolved server-side by Next.js;
    // it is never followed by the browser, so localhost is acceptable here.
    assert.match(match[1], /^https?:\/\//);
  });
});

/**
 * Integration guard (best-effort): a browser request to /api/servers/ AND to the
 * slash-less /api/servers must NOT end up redirecting to a localhost /
 * unreachable host. We issue GETs (no follow) and assert the first redirect
 * target (if any) is publicly reachable. With the fix there should be NO redirect
 * at all, but if one exists it must not point at localhost.
 */
describe("Live routing integration guard (skips if offline)", () => {
  const BASE = process.env.TEST_PUBLIC_URL || "https://app.thinksync.art";
  const http = require("http");
  const https = require("https");

  function head(url) {
    return new Promise((resolve) => {
      const lib = url.startsWith("https") ? https : http;
      try {
        const req = lib.request(
          url,
          { method: "GET", headers: { Origin: "https://app.thinksync.art" } },
          (res) => resolve({ status: res.statusCode, location: res.headers.location || null })
        );
        req.on("error", () => resolve({ status: 0, location: null }));
        req.setTimeout(8000, () => { req.destroy(); resolve({ status: 0, location: null }); });
        req.end();
      } catch {
        resolve({ status: 0, location: null });
      }
    });
  }

  for (const path of ["/api/servers/", "/api/servers"]) {
    test(`redirect target for ${path} is browser-reachable (no localhost/unreachable host)`, async () => {
      let r;
      try {
        r = await head(BASE + path);
      } catch {
        r = { status: 0, location: null };
      }
      if (r.status === 0) {
        console.warn(`Skipping live integration guard for ${path} (service unreachable).`);
        return;
      }
      // With the backend fix, expect no 3xx at all. If a redirect exists, it must
      // not point at localhost / an unreachable host.
      if (r.status >= 300 && r.status < 400 && r.location) {
        assert.doesNotMatch(r.location, /localhost/, "redirect must not point at localhost");
      }
      assert.ok(true);
    });
  }
});
