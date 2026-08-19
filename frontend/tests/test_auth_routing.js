/**
 * Regression test: Login → Register navigation must keep working whether or not
 * Next.js emits trailing-slash URLs.
 *
 * Historical root cause: a previous next.config.js set trailingSlash: true, so
 * client-side navigation resolved public routes as "/signup/", "/login/", etc.
 * AuthBootstrap.isPublicPath() compared against slash-less literals ("/signup",
 * "/login", …), so "/signup/" returned false → the guard treated the register
 * page as protected and called router.replace("/login") — the user bounced
 * straight back to login.
 *
 * The slash normalization in isPublicPath() is config-independent: it stays
 * correct after trailingSlash was removed (backend now owns slash parity). This
 * test asserts BOTH invariants:
 *   - public routes WITH and WITHOUT trailing slash are treated as public
 *   - protected routes still redirect to /login when unauthenticated
 */
const { test, describe } = require("node:test");
const assert = require("node:assert/strict");

// Replicate the exact guard logic from app/AuthBootstrap.tsx (kept in sync
// with the source). If this diverges from the component, the component must win.
function isPublicPath(pathname) {
  if (!pathname) return false;
  const normalized = pathname.replace(/\/+$/, "") || "/";
  return (
    normalized === "/" ||
    normalized === "/login" ||
    normalized === "/signup" ||
    normalized === "/demo"
  );
}

describe("AuthBootstrap.isPublicPath — public routes", () => {
  const publicPaths = ["/", "/login", "/signup", "/demo"];
  for (const p of publicPaths) {
    test(`"${p}" (no trailing slash) is public`, () => {
      assert.equal(isPublicPath(p), true);
    });
    test(`"${p}/" (trailing slash, trailingSlash:true) is public`, () => {
      assert.equal(isPublicPath(p + "/"), true, "trailing-slash variant must be public");
    });
    test(`"${p}//" (double slash) is public`, () => {
      assert.equal(isPublicPath(p + "//"), true);
    });
  }
});

describe("AuthBootstrap.isPublicPath — protected routes still gated", () => {
  const protectedPaths = [
    "/servers",
    "/servers/",
    "/dashboard",
    "/dashboard/",
    "/chat/abc",
    "/workspace/abc/chat",
    "/random",
  ];
  for (const p of protectedPaths) {
    test(`"${p}" is NOT public`, () => {
      assert.equal(isPublicPath(p), false);
    });
  }
});

// Logic guard mirroring the redirect condition in AuthBootstrap:
//   if (!token && !publicRoute) router.replace("/login")
// With no token, a public route must NOT redirect; a protected route must.
describe("Redirect decision (token absent)", () => {
  function wouldRedirect(pathname) {
    const token = null; // unauthenticated
    const publicRoute = isPublicPath(pathname);
    return !token && !publicRoute;
  }

  test("/signup/ does NOT redirect to /login (register works)", () => {
    assert.equal(wouldRedirect("/signup/"), false);
  });

  test("/login/ does NOT redirect to /login (login works)", () => {
    assert.equal(wouldRedirect("/login/"), false);
  });

  test("/servers/ DOES redirect to /login (protection intact)", () => {
    assert.equal(wouldRedirect("/servers/"), true);
  });

  test("/dashboard/ DOES redirect to /login (protection intact)", () => {
    assert.equal(wouldRedirect("/dashboard/"), true);
  });
});
