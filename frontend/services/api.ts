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
  display_name?: string | null;
  name: string;
  path: string;
  slug: string;
  domain: string;
  created_at: string;
  github_connection_id?: string | null;
}

// ── GitHub Connections ──────────────────────────────────────────────────────

export interface GitHubConnection {
  id: string;
  user_id: string;
  name: string;
  auth_method: string;
  host: string;
  ssh_public_key?: string | null;
  ssh_key_type?: string | null;
  created_at: string;
  updated_at: string;
}

// Returned ONLY once when the backend generates the keypair — the private
// key must be shown to the user exactly once so they can add it to GitHub.
export interface GitHubConnectionWithKey extends GitHubConnection {
  ssh_private_key: string;
}

export interface GitHubConnectionCreatePayload {
  name: string;
  auth_method?: string;
  // Import an existing keypair:
  ssh_private_key?: string;
  ssh_public_key?: string;
  // OR let the backend generate one (private key returned once):
  generate_keypair?: boolean;
  host?: string;
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

export interface WorkspaceCreatePayload {
  server_id: string;
  name: string;
  github_connection_id?: string | null;
  github_repo?: string | null;
  github_branch?: string | null;
  github_depth?: number | null;
}

export const createWorkspace = (payload: WorkspaceCreatePayload) =>
  request<Workspace>("/api/workspaces/", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const getWorkspace = (workspace_id: string) =>
  request<Workspace>(`/api/workspaces/${workspace_id}`);

export const getWorkspacesByServer = (server_id: string) =>
  request<Workspace[]>(`/api/workspaces/?server_id=${server_id}`);

// ── GitHub Connections ───────────────────────────────────────────────────

export const getGitHubConnections = () =>
  request<GitHubConnection[]>("/api/github-connections/");

export const createGitHubConnection = (data: GitHubConnectionCreatePayload) =>
  request<GitHubConnection | GitHubConnectionWithKey>("/api/github-connections/", {
    method: "POST",
    body: JSON.stringify(data),
  });

export const deleteGitHubConnection = (id: string) =>
  request<void>(`/api/github-connections/${id}`, { method: "DELETE" });

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
  deployment?: {
    url?: string | null;
    verified?: boolean | null;
    status?: string | null;
    message?: string | null;
    error?: string | null;
  } | null;
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
  type: "step_start" | "step_result" | "log_chunk" | "status_update" | "completed" | "ping" | "waiting_for_clarification";
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
  // Structured clarification form (new generic contract).
  questions?: ClarificationQuestion[];
  clarification_form?: ClarificationForm | null;
  turn?: number;
}

// ── Structured Clarification Form (generic renderer contract) ───────────────

export type ClarificationQuestionType =
  | "text"
  | "textarea"
  | "password"
  | "secret"
  | "number"
  | "boolean"
  | "single_select"
  | "multi_select"
  | "path"
  | "directory"
  | "url"
  | "domain"
  | "port"
  | "email"
  | "ssh_key"
  | "api_key"
  | "environment";

export interface ClarificationChoice {
  id: string;
  label: string;
  value: string;
  metadata?: Record<string, unknown>;
}

export interface ClarificationValidation {
  required: boolean;
  regex?: string | null;
  pattern_description?: string | null;
  min_length?: number | null;
  max_length?: number | null;
  min?: number | null;
  max?: number | null;
  allow_multi?: boolean;
}

export interface ClarificationFormQuestion {
  id: string;
  required_field: string;
  title: string;
  description: string;
  placeholder: string;
  example: string;
  required: boolean;
  secret: boolean;
  type: ClarificationQuestionType;
  default: unknown;
  choices: ClarificationChoice[];
  validation: ClarificationValidation;
  depends_on?: string | null;
  visible_if?: string | null;
  metadata?: Record<string, unknown>;
}

export interface ClarificationForm {
  id: string;
  title: string;
  description: string;
  questions: ClarificationFormQuestion[];
  metadata?: Record<string, unknown>;
}

export interface ClarificationFormAnswer {
  question_id: string;
  required_field: string;
  value: unknown;
  selected_choice?: string | null;
}

export interface ClarificationFormSubmission {
  clarification_id: string;
  answers: ClarificationFormAnswer[];
}

// Legacy free-text question shape (kept for backward compatibility).
export interface ClarificationQuestion {
  question_id?: string;
  question: string;
  question_type?: string;
  priority?: string;
  reason?: string;
  required_field?: string;
  blocking?: boolean;
  default_value?: unknown;
  validation_rule?: string;
  options?: string[];
  source?: string;
  cost_estimate?: number;
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

// New generic structured clarification submission (preferred).  Falls back to
// the free-text reply path when no submission is provided.
export const submitClarificationReply = (
  job_id: string,
  payload: {
    conversation_id?: string | null;
    reply?: string | null;
    clarification_submission?: ClarificationFormSubmission | null;
  }
) =>
  request<{ status: string; job_id: string; event: string; woke: boolean }>(
    `/api/agents/jobs/${job_id}/clarification-reply`,
    { method: "POST", body: JSON.stringify(payload) }
  );

export function getJobWebSocketUrl(jobId: string): string {
  const token = getToken();
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/v1/ws/jobs/${jobId}?token=${encodeURIComponent(token ?? "")}`;
}

// ── Agent phase status helpers (revived; consumed by AgentStatusBar / StepTimeline) ──

export type AgentPhase =
  | "queued"
  | "planning"
  | "reading_workspace"
  | "repository_analysis"
  | "implementation"
  | "running_commands"
  | "waiting_for_clarification"
  | "waiting_for_approval"
  | "waiting_for_resume"
  | "deploying"
  | "completed"
  | "failed";

export const AGENT_PHASE_LABELS: Record<AgentPhase, string> = {
  queued: "Queued",
  planning: "Planning",
  reading_workspace: "Reading Workspace",
  repository_analysis: "Repository Analysis",
  implementation: "Implementation",
  running_commands: "Running Commands",
  waiting_for_clarification: "Awaiting Clarification",
  waiting_for_approval: "Awaiting Approval",
  waiting_for_resume: "Awaiting Resume",
  deploying: "Deploying",
  completed: "Completed",
  failed: "Failed",
};

// Human-readable label for a workflow step tool + its args.
export function humanizeStep(tool: string | null, args?: Record<string, unknown> | null): string {
  const t = tool ?? "step";
  if (!args) return t;
  const target = (args.command as string) || (args.file as string) || (args.path as string);
  return target ? `${t}: ${target}` : t;
}
