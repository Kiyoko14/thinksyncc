const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function getAuthHeaders(): Record<string, string> {
  const token =
    typeof window !== "undefined" ? localStorage.getItem("thinksync_token") : null;
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      ...getAuthHeaders(),
      ...options.headers,
    },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(body.detail ?? "Request failed");
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

// ── Types ────────────────────────────────────────────────────────────────────

export interface Server {
  id: string;
  user_id: string;
  name: string;
  host: string;
  ssh_user: string;
  ssh_port: number;
  ssh_auth_method: "password" | "key";
  created_at: string;
}

export interface ServerCreatePayload {
  name: string;
  host: string;
  ssh_user: string;
  ssh_port: number;
  ssh_auth_method: "password" | "key";
  ssh_password?: string;
  ssh_key?: string;
}

export interface CommandResult {
  server_id: string;
  command: string;
  output: string;
  exit_code: number;
  executed_at: string;
}

// ── Servers ──────────────────────────────────────────────────────────────────

export const getServers = () => request<Server[]>("/api/v1/servers/");

export const addServer = (data: ServerCreatePayload) =>
  request<Server>("/api/v1/servers/", {
    method: "POST",
    body: JSON.stringify(data),
  });

export const deleteServer = (id: string) =>
  request<void>(`/api/v1/servers/${id}`, { method: "DELETE" });

// ── Commands ─────────────────────────────────────────────────────────────────

export const executeCommand = (server_id: string, command: string) =>
  request<CommandResult>("/api/v1/commands/execute", {
    method: "POST",
    body: JSON.stringify({ server_id, command }),
  });
