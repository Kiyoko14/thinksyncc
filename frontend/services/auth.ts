const TOKEN_KEY = "thinksync_token";

export type JwtPayload = Record<string, unknown> & { exp?: number; sub?: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function readResponseBody(response: Response): Promise<{ text: string; body: unknown }> {
  const text = await response.text();
  if (process.env.NODE_ENV !== "production") {
    // Debug-only: never leak raw response bodies (may contain tokens/PII) in prod.
    console.debug("RAW RESPONSE:", text);
  }

  if (!text.trim()) {
    return { text, body: null };
  }

  try {
    return { text, body: JSON.parse(text) };
  } catch {
    return { text, body: text };
  }
}

function extractErrorMessage(value: unknown): string | null {
  if (typeof value === "string") {
    const message = value.trim();
    return message || null;
  }

  if (!isRecord(value)) {
    return null;
  }

  const detailMessage = extractErrorMessage(value.detail);
  if (detailMessage) return detailMessage;

  const errorMessage = extractErrorMessage(value.error);
  if (errorMessage) return errorMessage;

  if (typeof value.message === "string" && value.message.trim()) {
    return value.message.trim();
  }

  if (typeof value.code === "string" && value.code.trim()) {
    return value.code.trim();
  }

  return null;
}

function buildErrorMessage(response: Response, body: unknown, text: string): string {
  const fromBody = extractErrorMessage(body);
  if (fromBody) return fromBody;

  const fromText = text.trim();
  if (fromText) return fromText;

  const statusText = response.statusText.trim();
  if (statusText) return `${response.status} ${statusText}`;

  return `HTTP ${response.status}`;
}

function base64UrlDecode(input: string): string {
  const normalized = input.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  if (typeof atob === "function") return atob(padded);
  // Fallback for non-browser runtimes (e.g., tests).
  return Buffer.from(padded, "base64").toString("utf-8");
}

export function decodeJwtPayload(token: string): JwtPayload | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;

  try {
    const json = base64UrlDecode(parts[1]);
    const payload = JSON.parse(json) as unknown;
    if (!payload || typeof payload !== "object") return null;
    return payload as JwtPayload;
  } catch {
    return null;
  }
}

export function isTokenExpired(token: string, skewSeconds = 15): boolean {
  const payload = decodeJwtPayload(token);
  const exp = payload?.exp;
  if (typeof exp !== "number") return true;
  const nowSeconds = Math.floor(Date.now() / 1000);
  return exp <= nowSeconds + skewSeconds;
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(TOKEN_KEY, token);
}

export async function login(email: string, password: string): Promise<void> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const { text, body } = await readResponseBody(response);

  if (!response.ok) {
    const error = new Error(buildErrorMessage(response, body, text));
    console.error("API ERROR:", error);
    throw error;
  }

  if (!isRecord(body) || typeof body.access_token !== "string") {
    const error = new Error(buildErrorMessage(response, body, text));
    console.error("API ERROR:", error);
    throw error;
  }

  const { access_token } = body as { access_token: string };
  setToken(access_token);
}

/**
 * Exchange a Google-issued OIDC ID token for a ThinkSync JWT.
 *
 * The Google Sign-In button (components/GoogleSignIn.tsx) uses the Google
 * Identity Services `useIdToken` flow to obtain a credential JWT signed by
 * Google. We forward that raw token to the backend, which verifies it
 * cryptographically and mints our own session JWT. No email/password is used.
 */
export async function googleLogin(idToken: string): Promise<void> {
  const response = await fetch("/api/auth/google", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id_token: idToken }),
  });
  const { text, body } = await readResponseBody(response);

  if (!response.ok) {
    const error = new Error(buildErrorMessage(response, body, text));
    console.error("GOOGLE LOGIN ERROR:", error);
    throw error;
  }

  if (!isRecord(body) || typeof body.access_token !== "string") {
    const error = new Error(buildErrorMessage(response, body, text));
    console.error("GOOGLE LOGIN ERROR:", error);
    throw error;
  }

  setToken(body.access_token as string);
}

export class AuthConfirmationRequiredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthConfirmationRequiredError";
  }
}

export async function register(email: string, password: string): Promise<void> {
  const response = await fetch("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const { text, body } = await readResponseBody(response);

  if (!response.ok) {
    const error = new Error(buildErrorMessage(response, body, text));
    console.error("API ERROR:", error);
    throw error;
  }

  if (!isRecord(body)) {
    const error = new Error(buildErrorMessage(response, body, text));
    console.error("API ERROR:", error);
    throw error;
  }

  // Email confirmation is enabled in Supabase: the backend returns
  // requires_confirmation=true and NO access_token. Do NOT store a token and
  // do NOT treat this as a completed login — the caller must show a
  // "check your email" screen instead of navigating into the app.
  if (body.requires_confirmation === true) {
    throw new AuthConfirmationRequiredError(
      "Please check your email to confirm your account before signing in.",
    );
  }

  if (typeof body.access_token !== "string") {
    const error = new Error(buildErrorMessage(response, body, text));
    console.error("API ERROR:", error);
    throw error;
  }

  const { access_token } = body as { access_token: string };
  setToken(access_token);
}

export function logout(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
}

export function validateStoredToken(skewSeconds = 15): string | null {
  if (typeof window === "undefined") return null;
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) return null;

  // Treat invalid/expired tokens as logged-out state (prevents UI desync).
  if (isTokenExpired(token, skewSeconds)) {
    logout();
    return null;
  }

  return token;
}

export function getToken(): string | null {
  return validateStoredToken();
}
