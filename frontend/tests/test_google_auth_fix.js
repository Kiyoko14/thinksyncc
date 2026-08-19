/**
 * Regression test for the Google-OAuth-only auth flow.
 *
 * After the migration, ThinkSync has NO email/password login. The browser
 * obtains a Google OIDC ID token and posts it to POST /api/auth/google; the
 * backend verifies it and returns a ThinkSync JWT that the client stores.
 *
 * This test mirrors the EXACT decision logic from services/auth.ts `googleLogin`
 * (kept in sync with the source). It does not import the .ts module (Node cannot
 * run TS without a loader); if the source logic diverges, the source must win
 * and this test updated. Uses Node's built-in test runner. Mocks global.fetch so
 * no backend/network is needed.
 */
const { test, describe } = require("node:test");
const assert = require("node:assert/strict");

const TOKEN_KEY = "thinksync_token";

function makeLocalStorage() {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
}

// ── Mirror of services/auth.ts googleLogin() branch logic ───────────────────
async function googleLogin(storage, fetchImpl, idToken) {
  const response = await fetchImpl("/api/auth/google", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id_token: idToken }),
  });
  const text = await response.text();
  let body = null;
  try {
    body = JSON.parse(text);
  } catch {
    /* keep null */
  }

  if (!response.ok) {
    const msg = body?.error || body?.message || text || `HTTP ${response.status}`;
    throw new Error(msg);
  }
  if (typeof body?.access_token !== "string") {
    throw new Error(body?.error || "bad response");
  }
  storage.setItem(TOKEN_KEY, body.access_token);
}

describe("googleLogin() — happy path", () => {
  test("stores the access_token returned by the backend", async () => {
    const storage = makeLocalStorage();
    const fetchImpl = async () => ({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () =>
        JSON.stringify({ access_token: "thinksync.jwt.token", token_type: "bearer" }),
    });
    await googleLogin(storage, fetchImpl, "google-id-token");
    assert.equal(storage.getItem(TOKEN_KEY), "thinksync.jwt.token");
  });
});

describe("googleLogin() — backend rejects the token", () => {
  test("throws a safe message and stores NO token", async () => {
    const storage = makeLocalStorage();
    const fetchImpl = async () => ({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
      text: async () =>
        JSON.stringify({ status: "error", error: "Google sign-in failed. Please try again." }),
    });
    await assert.rejects(
      async () => googleLogin(storage, fetchImpl, "bad-token"),
      (err) => err.message === "Google sign-in failed. Please try again.",
    );
    assert.equal(storage.getItem(TOKEN_KEY), null, "no token stored on failure");
  });
});
