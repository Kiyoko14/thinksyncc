"use client";

import { useEffect, useMemo, useRef, useState } from "react";

export type UiStatus = "idle" | "running" | "error";
export type ChatMode = "build" | "plan";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at?: string;
};

function StatusBadge({ status }: { status: UiStatus }) {
  const cfg = useMemo(() => {
    if (status === "running") return { dot: "bg-green-400", text: "Running", pill: "border-green-900/60 bg-green-950/40 text-green-200" };
    if (status === "error") return { dot: "bg-red-400", text: "Error", pill: "border-red-900/60 bg-red-950/40 text-red-200" };
    return { dot: "bg-gray-500", text: "Idle", pill: "border-gray-800 bg-gray-950/20 text-gray-300" };
  }, [status]);

  return (
    <span className={`inline-flex items-center gap-2 rounded-full border px-2.5 py-1 text-xs font-semibold ${cfg.pill}`}>
      <span className={`h-2 w-2 rounded-full ${cfg.dot}`} />
      {cfg.text}
    </span>
  );
}

export default function ChatWindow({
  title,
  messages,
  status,
  mode,
  onModeChange,
  onSend,
  disabled,
}: {
  title: string;
  messages: ChatMessage[];
  status: UiStatus;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  onSend: (text: string) => Promise<void> | void;
  disabled: boolean;
}) {
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, status]);

  const submit = async () => {
    const text = input.trim();
    if (!text || disabled || sending) return;
    setSending(true);
    setInput("");
    try {
      await onSend(text);
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-gray-800 px-4 py-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-white">{title}</p>
          <div className="mt-1 flex items-center gap-2">
            <StatusBadge status={status} />
            <div className="h-4 w-px bg-gray-800" />
            <div className="inline-flex rounded-xl border border-gray-800 bg-gray-950/30 p-1">
              <button
                onClick={() => onModeChange("plan")}
                className={`rounded-lg px-2.5 py-1 text-xs font-semibold transition ${
                  mode === "plan" ? "bg-gray-800 text-white" : "text-gray-400 hover:text-gray-200"
                }`}
              >
                Plan 🧠
              </button>
              <button
                onClick={() => onModeChange("build")}
                className={`rounded-lg px-2.5 py-1 text-xs font-semibold transition ${
                  mode === "build" ? "bg-gray-800 text-white" : "text-gray-400 hover:text-gray-200"
                }`}
              >
                Build ⚙️
              </button>
            </div>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="flex h-full items-center justify-center text-center">
            <div>
              <p className="text-sm font-semibold text-white">Ask the agent</p>
              <p className="mt-1 text-sm text-gray-500">Type an objective. Logs stream on the right.</p>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {messages.map((m) => (
              <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[78%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                    m.role === "user"
                      ? "bg-blue-600 text-white"
                      : "border border-gray-800 bg-gray-900/60 text-gray-100"
                  }`}
                >
                  <pre className="whitespace-pre-wrap font-sans">{m.content}</pre>
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      <div className="border-t border-gray-800 p-3">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={disabled ? "Select a chat…" : "Message…"}
            disabled={disabled || sending}
            rows={1}
            className="max-h-32 flex-1 resize-none rounded-2xl border border-gray-800 bg-gray-950/40 px-4 py-3 text-sm text-white placeholder:text-gray-600 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-60"
          />
          <button
            onClick={() => void submit()}
            disabled={disabled || sending || input.trim().length === 0}
            className="inline-flex h-11 items-center justify-center rounded-2xl bg-blue-600 px-4 text-sm font-semibold text-white transition hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {sending || status === "running" ? "Running…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

