// All requests go to relative paths — Next.js rewrites proxy them to the backend.
// See next.config.js → rewrites() for the INTERNAL_API_URL mapping.

import { getToken, logout } from "./auth";

export class ApiError extends Error {
  status: number;
  body: unknown;
  rawText: string;

  constructor(message: string, status: number, body: unknown, rawText = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.rawText = rawText;
  }
}

function handleUnauthorized(): void {
  if (typeof window === "undefined") return;

  logout();

  const currentPath = window.location.pathname;
  if (currentPath !== "/login") {
    window.location.replace("/login");
  }
}

function buildHeaders(options: RequestInit): Headers {
  const headers = new Headers(options.headers ?? {});

  // Default to JSON unless explicitly overridden (or using FormData).
  if (!headers.has("Content-Type") && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  // Never trust localStorage blindly: getToken() auto-clears invalid/expired tokens.
  const token = getToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  return headers;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function readResponseBody(response: Response): Promise<{ text: string; body: unknown }> {
  const text = await response.text();
  console.log("RESPONSE STATUS:", response.status, response.statusText);
  console.log("RAW RESPONSE:", text);

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

  if (Array.isArray(value.errors)) {
    const messages = value.errors.map(extractErrorMessage).filter((item): item is string => Boolean(item));
    if (messages.length > 0) {
      return messages.join("\n");
    }
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

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, headers: buildHeaders(options) });
  const { text, body } = response.status === 204 ? { text: "", body: null } : await readResponseBody(response);

  if (response.status === 401) {
    handleUnauthorized();
  }

  if (!response.ok) {
    const error = new ApiError(buildErrorMessage(response, body, text), response.status, body, text);
    console.error("API ERROR:", error);
    throw error;
  }

  if (response.status === 204) return undefined as T;

  if (isRecord(body) && body.status === "error") {
    const error = new ApiError(buildErrorMessage(response, body, text), response.status, body, text);
    console.error("API ERROR:", error);
    throw error;
  }

  if (isRecord(body) && body.status === "success" && "data" in body) {
    return body.data as T;
  }

  return body as T;
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

export interface CommandResult {
  server_id: string;
  command: string;
  output: string;
  exit_code: number;
  executed_at: string;
}

// ── Servers ──────────────────────────────────────────────────────────────────

export const getServers = () => request<Server[]>("/api/servers/");

export const addServer = (data: ServerCreatePayload) =>
  request<Server>("/api/servers/", {
    method: "POST",
    body: JSON.stringify(data),
  });

export const deleteServer = (id: string) =>
  request<void>(`/api/servers/${id}`, { method: "DELETE" });

// ── Workspaces ────────────────────────────────────────────────────────────────

export const createWorkspace = (server_id: string, name: string) =>
  request<Workspace>("/api/workspaces/", {
    method: "POST",
    body: JSON.stringify({ server_id, name }),
  });

export const getWorkspace = (workspace_id: string) =>
  request<Workspace>(`/api/workspaces/${workspace_id}`);

export const getWorkspacesByServer = (server_id: string) =>
  request<Workspace[]>(`/api/workspaces/?server_id=${server_id}`);

// ── Commands ──────────────────────────────────────────────────────────────────

export const executeCommand = (workspace_id: string, server_id: string, command: string) =>
  request<CommandResult>("/api/commands/execute", {
    method: "POST",
    body: JSON.stringify({ workspace_id, server_id, command }),
  });

// ── Agents (Forge v2) ────────────────────────────────────────────────────────

export type AgentJobStatus =
  | "queued"
  | "running"
  | "waiting_for_llm"
  | "completed"
  | "failed";

export interface ForgeV2RunRequest {
  workspace_id: string;
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
  reason?: string;
  rationale?: string;
  risk_level?: string;
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

export interface JobRecord {
  id: string;
  workspace_id?: string | null;
  server_id: string;
  objective: string;
  status: AgentJobStatus;
  allow_write: boolean;
  dry_run: boolean;
  task_mode: "simple" | "complex";
  plan: AgentStep[];
  steps: StepResult[];
  decisions: AgentDecision[];
  summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobStreamEvent {
  type: "step_start" | "step_result" | "log_chunk" | "status_update" | "completed" | "ping";
  sequence?: number;
  status?: AgentJobStatus;
  step: number;
  tool: string | null;
  timestamp?: string;
  args?: Record<string, unknown>;
  stream?: "stdout" | "stderr";
  data?: string;
  stdout_preview?: string;
  stderr_preview?: string;
  success?: boolean;
  exit_code?: number;
  summary?: string;
  decision?: AgentDecision;
  task_mode?: "simple" | "complex";
  plan?: AgentStep[];
}

export type ChatRole = "user" | "assistant" | "system";

export interface StoredChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  created_at: string;
  chat_id?: string | null;
  workspace_id?: string | null;
  user_id?: string | null;
}

export interface WorkspaceChatResponse {
  id: string;
  workspace_id: string;
  user_id: string;
  created_at: string;
  messages: StoredChatMessage[];
}

export interface ChatSendMessageResponse {
  chat_id: string;
  workspace_id: string;
  response: string;
  message: StoredChatMessage;
}

export const sendWorkspaceMessage = (workspace_id: string, message: string, role: ChatRole = "user") =>
  request<ChatSendMessageResponse>(`/api/chat/${workspace_id}/message`, {
    method: "POST",
    body: JSON.stringify({ message, role }),
  });

export const runForgeV2 = (payload: ForgeV2RunRequest) =>
  request<ForgeV2JobResponse>("/api/agents/forge-v2/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const getForgeV2JobStatus = (job_id: string) =>
  request<ForgeV2JobResponse>(`/api/agents/forge-v2/jobs/${job_id}`);

export const getForgeV2Plan = (payload: ForgeV2RunRequest) =>
  request<{ plan: AgentStep[]; objective: string; context_summary: string }>(
    "/api/agents/forge-v2/plan",
    { method: "POST", body: JSON.stringify(payload) }
  );

export const runPlanModeChat = (objective: string) =>
  request<{ type: "chat"; message: string }>("/api/agents/route", {
    method: "POST",
    body: JSON.stringify({ mode: "plan", objective }),
  });

export const getWorkspaceChat = (workspace_id: string) =>
  request<WorkspaceChatResponse>(`/api/chat/${workspace_id}`);

export const getWorkspaceJobs = (workspace_id: string) =>
  request<JobRecord[]>(`/api/jobs/?workspace_id=${workspace_id}`);

export function getJobWebSocketUrl(jobId: string): string {
  const token = getToken();
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/v1/ws/jobs/${jobId}?token=${encodeURIComponent(token ?? "")}`;
}
