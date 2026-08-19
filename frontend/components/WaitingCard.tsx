"use client";

import { useState } from "react";
import { HelpCircle, ShieldCheck, RotateCcw, Send, Loader2 } from "lucide-react";
import type { ClarificationQuestion } from "@/services/api";

export type WaitKind = "clarification" | "approval" | "resume";

/**
 * Dedicated action card shown when the agent suspends for user input.
 * Clearly states WHY it stopped and WHAT is required; disappears after submit
 * (parent clears the pending wait on success).
 */
export default function WaitingCard({
  kind,
  questions,
  prompt,
  onSubmit,
  onCancel,
  submitting,
  error,
}: {
  kind: WaitKind;
  questions?: ClarificationQuestion[];
  prompt?: string;
  onSubmit: (reply: string) => void;
  onCancel?: () => void;
  submitting: boolean;
  error: string | null;
}) {
  const [reply, setReply] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const icon =
    kind === "clarification" ? <HelpCircle size={18} className="text-amber-600" /> :
    kind === "approval" ? <ShieldCheck size={18} className="text-amber-600" /> :
    <RotateCcw size={18} className="text-amber-600" />;

  const title =
    kind === "clarification" ? "Agent needs clarification" :
    kind === "approval" ? "Agent waiting for approval" :
    "Agent waiting to resume";

  const subtitle =
    kind === "clarification" ? "The agent paused to ask you something before continuing." :
    kind === "approval" ? "The agent needs your go-ahead before this step." :
    "The agent is paused and ready to continue when you respond.";

  const resolvedReply =
    kind === "approval" && selected ? selected :
    reply.trim();

  const canSubmit = !submitting && resolvedReply.length > 0;

  const firstOptions = questions?.[0]?.options;
  const useChoice = kind === "approval" && firstOptions && firstOptions.length > 0;

  return (
    <div
      className="app-panel border-amber-200 bg-amber-50/90 p-4 shadow-sm shadow-amber-100/60"
      role="alertdialog"
      aria-label={title}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5">{icon}</div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-amber-950">{title}</p>
          <p className="mt-0.5 text-sm text-amber-800">{subtitle}</p>
        </div>
      </div>

      <div className="mt-3 rounded-2xl border border-amber-200 bg-white/80 px-3 py-2 text-sm text-slate-700">
        ThinkSync needs one decision before it can continue.
      </div>

      {questions && questions.length > 0 ? (
        <ul className="mt-3 space-y-2">
          {questions.map((q, i) => (
            <li key={i} className="rounded-2xl border border-slate-200 bg-white/80 px-3 py-3 text-sm text-slate-700">
              <p className="font-medium text-slate-900">{q.question}</p>
              {q.options && q.options.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {q.options.map((opt) => (
                    <button
                      key={opt}
                      type="button"
                      onClick={() => setReply(opt)}
                      className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
                        reply === opt
                          ? "border-amber-500 bg-amber-100 text-amber-900"
                          : "border-slate-300 bg-white text-slate-600 hover:border-amber-400"
                      }`}
                    >
                      {opt}
                    </button>
                  ))}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : prompt ? (
        <p className="mt-3 rounded-2xl border border-slate-200 bg-white/80 px-3 py-2 text-sm text-slate-700">{prompt}</p>
      ) : null}

      {useChoice ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {firstOptions!.map((opt) => (
            <button
              key={opt}
              type="button"
              disabled={submitting}
              onClick={() => setSelected(opt)}
              className={`rounded-2xl border px-4 py-2.5 text-sm font-semibold transition ${
                selected === opt
                  ? "border-amber-500 bg-amber-500 text-white"
                  : "border-slate-300 bg-white text-slate-700 hover:border-amber-400"
              }`}
            >
              {opt}
            </button>
          ))}
        </div>
      ) : (
        <textarea
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          disabled={submitting}
          rows={3}
          placeholder={kind === "approval" ? "Add a note (optional) or approve…" : "Type your answer…"}
          className="mt-3 w-full resize-none rounded-2xl border border-amber-200 bg-white px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-amber-400 focus:outline-none focus:ring-2 focus:ring-amber-100"
        />
      )}

      {error ? (
        <p className="mt-2 rounded-2xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
      ) : null}

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          disabled={!canSubmit}
          onClick={() => onSubmit(resolvedReply)}
          className="inline-flex items-center gap-2 rounded-2xl bg-amber-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          {kind === "approval" ? "Approve" : "Send"}
        </button>
        {onCancel ? (
          <button
            type="button"
            disabled={submitting}
            onClick={onCancel}
            className="rounded-2xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-600 transition hover:bg-slate-50"
          >
            Dismiss
          </button>
        ) : null}
      </div>
    </div>
  );
}
