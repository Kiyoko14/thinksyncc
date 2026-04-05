const TOKEN_KEY = "thinksync_token";

export async function login(email: string, password: string): Promise<void> {
  const response = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "Login failed" }));
    throw new Error(body.detail ?? "Login failed");
  }

  const { access_token } = (await response.json()) as { access_token: string };
  localStorage.setItem(TOKEN_KEY, access_token);
}

export async function register(email: string, password: string): Promise<void> {
  const response = await fetch("/api/v1/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "Registration failed" }));
    throw new Error(body.detail ?? "Registration failed");
  }

  const { access_token } = (await response.json()) as { access_token: string };
  localStorage.setItem(TOKEN_KEY, access_token);
}

export function logout(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}
