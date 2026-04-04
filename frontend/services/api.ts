// All requests go to relative paths — Next.js rewrites proxy them to the backend.
// See next.config.js → rewrites() for the INTERNAL_API_URL mapping.

function getAuthHeaders(): Record<string, string> {
  const token =
    typeof window !== "undefined" ? localStorage.getItem("thinksync_token") : null;
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
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

export interface Workspace {
  id: string;
  user_id: string;
  server_id: string;
  name: string;
  path: string;
  slug: string;
  domain: string;
  created_at: string;
}

export interface ChatMessage {
  id: string;
  chat_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
}

export interface ChatHistory {
  id: string;
  workspace_id: string;
  user_id: string;
  created_at: string;
  messages: ChatMessage[];
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

// ── Workspaces ────────────────────────────────────────────────────────────────

export const createWorkspace = (server_id: string, name: string) =>
  request<Workspace>("/api/v1/workspaces/", {
    method: "POST",
    body: JSON.stringify({ server_id, name }),
  });

export const getWorkspacesByServer = (server_id: string) =>
  request<Workspace[]>(`/api/v1/workspaces/?server_id=${server_id}`);

// ── Chat ──────────────────────────────────────────────────────────────────────

export const getChatHistory = (workspace_id: string) =>
  request<ChatHistory>(`/api/v1/chat/${workspace_id}`);

export const sendChatMessage = (workspace_id: string, message: string) =>
  request<{ chat_id: string; workspace_id: string; response: string }>(
    `/api/v1/chat/${workspace_id}/message`,
    {
      method: "POST",
      body: JSON.stringify({ message }),
    }
  );

// ── Commands ──────────────────────────────────────────────────────────────────

export const executeCommand = (server_id: string, command: string) =>
  request<CommandResult>("/api/v1/commands/execute", {
    method: "POST",
    body: JSON.stringify({ server_id, command }),
  });
