"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  ApiError,
  getForgeV2JobStatus,
  getJobWebSocketUrl,
  getWorkspace,
  getWorkspaceChat,
  getWorkspaceJobs,
  runForgeV2,
} from "@/services/api";
import { getToken } from "@/services/auth";
import type {
  AgentJobStatus,
  ForgeV2JobResponse,
  JobRecord,
  JobStreamEvent,
  StepResult,
  StoredChatMessage,
  Workspace,
} from "@/services/api";

const POLL_INTERVAL_MS = 2000;
const MAX_STEP_OUTPUT_CHARS = 4000;

type ChatRole = "user" | "assistant" | "system";

interface LiveStep extends StepResult {
  pending?: boolean;
}

interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  status?: AgentJobStatus;
  jobId?: string;
  steps?: LiveStep[];
  isError?: boolean;
}

function createMessageId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function toChatMessage(message: StoredChatMessage): ChatMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    createdAt: message.created_at,
  };
}

function formatToolName(tool: string): string {
  return tool
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function truncateOutput(output: string): string {
  if (output.length <= MAX_STEP_OUTPUT_CHARS) {
    return output;
  }
  return `${output.slice(0, MAX_STEP_OUTPUT_CHARS)}\n…`;
}

function buildAssistantMessage(job: ForgeV2JobResponse): Pick<ChatMessage, "content" | "status" | "steps" | "jobId" | "isError"> {
  if (job.status === "failed") {
    return {
      content: job.error ?? job.run?.summary ?? "Agent run failed.",
      status: job.status,
      steps: job.run?.results ?? [],
      jobId: job.job_id,
      isError: true,
    };
  }

  if (job.status === "completed") {
    return {
      content: job.run?.summary || "Agent run completed.",
      status: job.status,
      steps: job.run?.results ?? [],
      jobId: job.job_id,
      isError: false,
    };
  }

  return {
    content: "Agent is running...",
    status: job.status,
    steps: job.run?.results ?? [],
    jobId: job.job_id,
    isError: false,
  };
}

function buildAssistantMessageFromJob(job: JobRecord): ChatMessage {
  return {
    id: `job-${job.id}`,
    role: "assistant",
    content: job.summary || "Agent is running...",
    createdAt: job.created_at,
    status: job.status,
    jobId: job.id,
    steps: job.steps,
    isError: job.status === "failed",
  };
}

function upsertStep(steps: LiveStep[], nextStep: LiveStep): LiveStep[] {
  const existingIndex = steps.findIndex((step) => step.step === nextStep.step);
  if (existingIndex === -1) {
    return [...steps, nextStep].sort((left, right) => left.step - right.step);
  }

  const clone = [...steps];
  clone[existingIndex] = { ...clone[existingIndex], ...nextStep };
  return clone;
}

export default function ChatPage() {
  const router = useRouter();
  const params = useParams();
  const workspaceId = params.workspaceId as string;

  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const pollTimeoutRef = useRef<number | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const lastSequenceRef = useRef<number>(0);
  const mountedRef = useRef(true);

  const isAgentRunning = submitting || activeJobId !== null;

  const getCachedServerId = () => {
    if (typeof window === "undefined") return null;
    try {
      return localStorage.getItem(`thinksync_workspace_server:${workspaceId}`) || null;
    } catch {
      return null;
    }
  };

  const stopPolling = () => {
    if (pollTimeoutRef.current !== null) {
      window.clearTimeout(pollTimeoutRef.current);
      pollTimeoutRef.current = null;
    }
  };

  const closeSocket = () => {
    socketRef.current?.close();
    socketRef.current = null;
  };

  const updateMessage = (messageId: string, patch: Partial<ChatMessage>) => {
    setMessages((prev) =>
      prev.map((message) =>
        message.id === messageId ? { ...message, ...patch } : message,
      ),
    );
  };

  const upsertAssistantMessage = (message: ChatMessage) => {
    setMessages((prev) => {
      const existingIndex = prev.findIndex((item) => item.id === message.id);
      if (existingIndex === -1) {
        return [...prev, message];
      }

      const clone = [...prev];
      clone[existingIndex] = { ...clone[existingIndex], ...message };
      return clone;
    });
  };

  const applyJobUpdate = (messageId: string, job: ForgeV2JobResponse) => {
    updateMessage(messageId, buildAssistantMessage(job));
  };

  const appendLogChunk = (messageId: string, event: JobStreamEvent) => {
    if (!event.data || !event.tool) return;

    setMessages((prev) =>
      prev.map((message) => {
        if (message.id !== messageId) return message;
        const steps = message.steps ? [...message.steps] : [];
        const targetStep = steps.find((step) => step.step === event.step) ?? {
          step: event.step,
          tool: event.tool ?? "unknown",
          args: {},
          stdout: "",
          stderr: "",
          exit_code: 0,
          duration_ms: 0,
          executed_at: event.timestamp ?? new Date().toISOString(),
          success: false,
          pending: true,
        };

        const patch =
          event.stream === "stderr"
            ? { stderr: `${targetStep.stderr}${event.data}` }
            : { stdout: `${targetStep.stdout}${event.data}` };

	        return {
	          ...message,
	          steps: upsertStep(steps, {
	            ...targetStep,
	            ...patch,
	            tool: (targetStep.tool ?? "unknown"),
	            pending: true,
	          }),
	        };
	      }),
	    );
	  };

  const applyJobStreamEvent = (messageId: string, event: JobStreamEvent) => {
    if (event.sequence && event.sequence <= lastSequenceRef.current) {
      return;
    }
    if (event.sequence) {
      lastSequenceRef.current = event.sequence;
    }

    if (event.type === "ping") {
      return;
    }

    if (event.type === "status_update") {
      updateMessage(messageId, {
        status: event.status,
        content:
          event.status === "waiting_for_llm"
            ? "Agent is evaluating the last result..."
            : "Agent is running...",
      });
      return;
    }

    if (event.type === "step_start" && event.tool) {
      setMessages((prev) =>
        prev.map((message) => {
          if (message.id !== messageId) return message;
          const steps = message.steps ? [...message.steps] : [];
          const nextStep: LiveStep = {
            step: event.step,
            tool: event.tool ?? "unknown",
            args: (event.args as Record<string, unknown>) ?? {},
            stdout: "",
            stderr: "",
            exit_code: 0,
            duration_ms: 0,
            executed_at: event.timestamp ?? new Date().toISOString(),
            success: false,
            pending: true,
          };
          return {
            ...message,
            status: event.status ?? "running",
            steps: upsertStep(steps, nextStep),
          };
        }),
      );
      return;
    }

    if (event.type === "log_chunk") {
      appendLogChunk(messageId, event);
      return;
    }

    if (event.type === "step_result" && event.tool) {
      setMessages((prev) =>
        prev.map((message) => {
          if (message.id !== messageId) return message;
          const steps = message.steps ? [...message.steps] : [];
          const current = steps.find((step) => step.step === event.step);
          const nextStep: LiveStep = {
            step: event.step,
            tool: event.tool ?? "unknown",
            args: current?.args ?? {},
            stdout: current?.stdout ?? "",
            stderr: current?.stderr ?? "",
            exit_code: event.exit_code ?? 0,
            duration_ms: current?.duration_ms ?? 0,
            executed_at: current?.executed_at ?? event.timestamp ?? new Date().toISOString(),
            success: Boolean(event.success),
            pending: false,
          };
          return {
            ...message,
            status: event.status ?? "running",
            steps: upsertStep(steps, nextStep),
          };
        }),
      );
      return;
    }

    if (event.type === "completed") {
      updateMessage(messageId, {
        content: event.summary || "Agent run completed.",
        status: event.success ? "completed" : "failed",
        isError: !event.success,
      });
      stopPolling();
      closeSocket();
      setActiveJobId(null);
      inputRef.current?.focus();
    }
  };

  const pollJob = async (jobId: string, messageId: string) => {
    try {
      const job = await getForgeV2JobStatus(jobId);
      if (!mountedRef.current) return;

      applyJobUpdate(messageId, job);

      if (job.status === "completed" || job.status === "failed") {
        stopPolling();
        setActiveJobId(null);
        inputRef.current?.focus();
        return;
      }

      pollTimeoutRef.current = window.setTimeout(() => {
        void pollJob(jobId, messageId);
      }, POLL_INTERVAL_MS);
    } catch (err: unknown) {
      if (!mountedRef.current) return;

      stopPolling();
      setActiveJobId(null);

      updateMessage(messageId, {
        content: err instanceof Error ? err.message : "Failed to poll agent job",
        status: "failed",
        isError: true,
      });
      setError("Failed to refresh agent status");
      inputRef.current?.focus();
    }
  };

  const connectJobStream = (jobId: string, messageId: string) => {
    closeSocket();
    stopPolling();
    lastSequenceRef.current = 0;

    try {
      const socket = new WebSocket(getJobWebSocketUrl(jobId));
      socketRef.current = socket;

      socket.onmessage = (message) => {
        const event = JSON.parse(message.data) as JobStreamEvent;
        applyJobStreamEvent(messageId, event);
      };

      socket.onerror = () => {
        closeSocket();
        if (mountedRef.current) {
          pollTimeoutRef.current = window.setTimeout(() => {
            void pollJob(jobId, messageId);
          }, POLL_INTERVAL_MS);
        }
      };

      socket.onclose = () => {
        if (!mountedRef.current || activeJobId !== jobId) return;
        if (socketRef.current === socket) {
          socketRef.current = null;
        }
      };
    } catch {
      pollTimeoutRef.current = window.setTimeout(() => {
        void pollJob(jobId, messageId);
      }, POLL_INTERVAL_MS);
    }
  };

  useEffect(() => {
    mountedRef.current = true;

    return () => {
      mountedRef.current = false;
      stopPolling();
      closeSocket();
    };
  }, []);

  useEffect(() => {
    if (!workspaceId) {
      router.replace("/servers");
      return;
    }
    if (!getToken()) {
      router.replace("/login");
      return;
    }

    let cancelled = false;

    const loadWorkspace = async () => {
      stopPolling();
      closeSocket();

      setActiveJobId(null);
      setLoading(true);
      setError("");
      setWorkspace(null);

      try {
        const nextWorkspace = await getWorkspace(workspaceId);
        if (cancelled || !mountedRef.current) return;

        setWorkspace(nextWorkspace);

        const [chat, jobs] = await Promise.all([
          getWorkspaceChat(workspaceId),
          getWorkspaceJobs(workspaceId),
        ]);
        if (cancelled || !mountedRef.current) return;

        setMessages((chat?.messages ?? []).map(toChatMessage));

        const activeJob = jobs.find(
          (job) => job.status === "queued" || job.status === "running" || job.status === "waiting_for_llm",
        );

        if (activeJob) {
          const assistantMessage = buildAssistantMessageFromJob(activeJob);
          upsertAssistantMessage(assistantMessage);
          setActiveJobId(activeJob.id);
          connectJobStream(activeJob.id, assistantMessage.id);
        }
      } catch (err: unknown) {
        if (cancelled || !mountedRef.current) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 400)) {
          const serverId = getCachedServerId();
          router.replace(serverId ? `/servers/${serverId}/workspaces` : "/servers");
          return;
        }
        setError(err instanceof Error ? err.message : "Failed to load workspace");
      } finally {
        if (!cancelled && mountedRef.current) {
          setLoading(false);
        }
      }
    };

    void loadWorkspace();

    return () => {
      cancelled = true;
      stopPolling();
      closeSocket();
    };
  }, [workspaceId, router]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isAgentRunning]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !workspace || isAgentRunning) return;

    const createdAt = new Date().toISOString();
    const userMessage: ChatMessage = {
      id: createMessageId(),
      role: "user",
      content: text,
      createdAt,
    };
    const assistantMessageId = createMessageId();
    const assistantMessage: ChatMessage = {
      id: assistantMessageId,
      role: "assistant",
      content: "Agent is running...",
      createdAt,
      status: "queued",
      steps: [],
      isError: false,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setInput("");
    setSubmitting(true);
    setError("");

    try {
      const job = await runForgeV2({
        workspace_id: workspace.id,
        server_id: workspace.server_id,
        objective: text,
        max_steps: 6,
        allow_write: false,
      });

      if (!mountedRef.current) return;

      setSubmitting(false);
      setActiveJobId(job.job_id);
      applyJobUpdate(assistantMessageId, job);
      connectJobStream(job.job_id, assistantMessageId);
    } catch (err: unknown) {
      if (!mountedRef.current) return;

      setSubmitting(false);

      const message =
        err instanceof Error ? err.message : "Failed to start agent run";

      updateMessage(assistantMessageId, {
        content: message,
        status: "failed",
        isError: true,
      });
      setError(message);
      inputRef.current?.focus();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSend();
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    e.target.style.height = "auto";
    e.target.style.height = `${Math.min(e.target.scrollHeight, 120)}px`;
  };

  if (loading && !workspace) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-950 text-white">
        <svg className="h-6 w-6 animate-spin text-blue-500" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
        </svg>
      </div>
    );
  }

  if (!workspace) {
    return null;
  }

  return (
    <div className="flex h-screen flex-col bg-gray-950 text-white">
      <header className="safe-top flex h-14 flex-shrink-0 items-center gap-3 border-b border-gray-800 bg-gray-950/95 px-4 backdrop-blur">
        <button
          onClick={() => router.push(workspace ? `/servers/${workspace.server_id}/workspaces` : "/servers")}
          className="p-1 -ml-1 text-gray-400 transition-colors hover:text-white"
          aria-label="Go back"
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="15 18 9 12 15 6" />
          </svg>
        </button>
        <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-blue-600/30 bg-blue-600/20">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
          </svg>
        </div>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-white">ThinkSync Agent</p>
          <p className="truncate text-[11px] text-gray-500">
            {workspace ? `${workspace.name} · ${workspace.server_id.slice(0, 8)}…` : "Loading workspace…"}
          </p>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {loading ? (
          <div className="flex h-full items-center justify-center">
            <svg className="h-6 w-6 animate-spin text-blue-500" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
            </svg>
          </div>
        ) : messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center px-6 text-center">
            <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-2xl border border-blue-600/20 bg-blue-600/10">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              </svg>
            </div>
            <p className="mb-1 font-semibold text-white">Run a real agent task</p>
            <p className="text-sm text-gray-500">
              Ask the agent to inspect your server. The backend will execute real commands and stream the results here.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {messages.map((message) => (
              <div
                key={message.id}
                className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}
              >
                {message.role === "assistant" && (
                  <div className="mr-2 mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg border border-blue-600/30 bg-blue-600/20">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" strokeWidth="2" strokeLinecap="round">
                      <circle cx="12" cy="12" r="3" />
                      <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
                    </svg>
                  </div>
                )}
                <div
                  className={`max-w-[82%] rounded-2xl px-4 py-3 text-sm leading-relaxed break-words ${
                    message.role === "user"
                      ? "rounded-br-sm bg-blue-600 text-white"
                      : message.isError
                        ? "rounded-bl-sm border border-red-800 bg-red-950/60 text-red-100"
                        : "rounded-bl-sm border border-gray-700 bg-gray-800 text-gray-100"
                  }`}
                >
                  <p className="whitespace-pre-wrap">{message.content}</p>

                  {message.role === "assistant" && message.status && message.status !== "completed" && !message.isError && (
                    <p className="mt-2 text-[11px] uppercase tracking-[0.18em] text-blue-300/80">
                      {message.status === "queued" ? "Queued" : "In progress"}
                    </p>
                  )}

                  {message.steps && message.steps.length > 0 && (
                    <div className="mt-3 space-y-2 border-t border-white/10 pt-3">
                      {message.steps.map((step) => (
                        <div
                          key={`${message.id}-${step.step}-${step.executed_at}`}
                          className="rounded-xl border border-white/10 bg-black/20 p-3"
                        >
                          <div className="flex items-center justify-between gap-3">
                            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-gray-300">
                              Step {step.step} · {formatToolName(step.tool)}
                            </p>
                            <span
                              className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] ${
                                step.pending
                                  ? "bg-blue-500/15 text-blue-300"
                                  : step.success
                                    ? "bg-emerald-500/15 text-emerald-300"
                                    : "bg-red-500/15 text-red-300"
                              }`}
                            >
                              {step.pending ? "running" : step.success ? "ok" : `exit ${step.exit_code}`}
                            </span>
                          </div>

                          {step.stdout && (
                            <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-black/30 p-2 text-xs text-gray-100">
                              {truncateOutput(step.stdout)}
                            </pre>
                          )}

                          {step.stderr && (
                            <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-red-950/40 p-2 text-xs text-red-200">
                              {truncateOutput(step.stderr)}
                            </pre>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {error && !loading && (
          <div className="mt-4 flex justify-center">
            <div className="rounded-xl border border-red-800 bg-red-950/60 px-4 py-2">
              <p className="text-xs text-red-400">{error}</p>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <div className="safe-bottom flex-shrink-0 border-t border-gray-800 bg-gray-950 px-4 py-3">
        <div className="flex items-end gap-3 rounded-2xl border border-gray-700 bg-gray-900 px-4 py-2 transition-colors focus-within:border-blue-500">
          <textarea
            ref={inputRef}
            rows={1}
            value={input}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            placeholder={isAgentRunning ? "Agent is running..." : "Ask the agent to inspect your server…"}
            disabled={loading || !workspace || isAgentRunning}
            className="max-h-[120px] flex-1 resize-none bg-transparent py-1.5 text-base leading-relaxed text-white placeholder:text-gray-600 focus:outline-none"
            style={{ height: "auto" }}
          />
          <button
            onClick={() => void handleSend()}
            disabled={!input.trim() || loading || !workspace || isAgentRunning}
            className="mb-0.5 flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-blue-600 transition-colors hover:bg-blue-500 active:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
            aria-label="Send message"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        </div>
        <p className="mt-2 text-center text-[10px] text-gray-700">
          {isAgentRunning ? "Agent is streaming live output..." : "Enter to send · Shift+Enter for new line"}
        </p>
      </div>
    </div>
  );
}
