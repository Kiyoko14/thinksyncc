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

// ── Agents (Forge v2) ────────────────────────────────────────────────────────

export type AgentJobStatus =
  | "queued"
  | "running"
  | "waiting_for_llm"
  | "completed"
  | "failed";

export interface ForgeV2RunRequest {
  server_id: string;
  objective: string;
  max_steps?: number;
  max_retries?: number;
  allow_write?: boolean;
  dry_run?: boolean;
  step_timeout_seconds?: number;
}

export interface AgentStep {
  step: number;
  tool: string;
  args: Record<string, unknown>;
  rationale: string;
}

export interface StepResult {
  step: number;
  tool: string;
  args: Record<string, unknown>;
  stdout: string;
  stderr: string;
  exit_code: number;
  duration_ms: number;
  executed_at: string;
  success: boolean;
}

export interface AgentDecision {
  action: "continue" | "retry" | "modify" | "abort";
  reason: string;
  modified_step: AgentStep | null;
  summary_so_far: string;
}

export interface ForgeV2RunResponse {
  agent: string;
  job_id: string;
  objective: string;
  dry_run: boolean;
  plan: AgentStep[];
  results: StepResult[];
  decisions: AgentDecision[];
  summary: string;
  success: boolean;
}

export interface ForgeV2JobResponse {
  job_id: string;
  status: AgentJobStatus;
  run: ForgeV2RunResponse | null;
  error: string | null;
}

export const runForgeV2 = (payload: ForgeV2RunRequest) =>
  request<ForgeV2JobResponse>("/api/v1/agents/forge-v2/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const getForgeV2JobStatus = (job_id: string) =>
  request<ForgeV2JobResponse>(`/api/v1/agents/forge-v2/jobs/${job_id}`);

export const getForgeV2Plan = (payload: ForgeV2RunRequest) =>
  request<{ plan: AgentStep[]; objective: string; context_summary: string }>(
    "/api/v1/agents/forge-v2/plan",
    { method: "POST", body: JSON.stringify(payload) }
  );
