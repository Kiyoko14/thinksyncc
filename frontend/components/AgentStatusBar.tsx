"use client";

import { Loader2, CheckCircle2, XCircle, HelpCircle, ShieldCheck, RotateCcw, Rocket } from "lucide-react";
import type { AgentPhase } from "@/services/api";
import { AGENT_PHASE_LABELS } from "@/services/api";

const PHASE_STYLE: Record<AgentPhase, { dot: string; pill: string; icon: React.ReactNode }> = {
  queued: {
    dot: "bg-slate-400",
    pill: "border-slate-200 bg-slate-100 text-slate-700",
    icon: <Loader2 size={14} className="animate-spin" />,
  },
  planning: {
    dot: "bg-indigo-400",
    pill: "border-indigo-200 bg-indigo-50 text-indigo-700",
    icon: <Loader2 size={14} className="animate-spin" />,
  },
  reading_workspace: {
    dot: "bg-sky-400",
    pill: "border-sky-200 bg-sky-50 text-sky-700",
    icon: <Loader2 size={14} className="animate-spin" />,
  },
  repository_analysis: {
    dot: "bg-violet-400",
    pill: "border-violet-200 bg-violet-50 text-violet-700",
    icon: <Loader2 size={14} className="animate-spin" />,
  },
  implementation: {
    dot: "bg-blue-400",
    pill: "border-blue-200 bg-blue-50 text-blue-700",
    icon: <Loader2 size={14} className="animate-spin" />,
  },
  running_commands: {
    dot: "bg-blue-400",
    pill: "border-blue-200 bg-blue-50 text-blue-700",
    icon: <Loader2 size={14} className="animate-spin" />,
  },
  waiting_for_clarification: {
    dot: "bg-amber-400",
    pill: "border-amber-200 bg-amber-50 text-amber-800",
    icon: <HelpCircle size={14} />,
  },
  waiting_for_approval: {
    dot: "bg-amber-400",
    pill: "border-amber-200 bg-amber-50 text-amber-800",
    icon: <ShieldCheck size={14} />,
  },
  waiting_for_resume: {
    dot: "bg-amber-400",
    pill: "border-amber-200 bg-amber-50 text-amber-800",
    icon: <RotateCcw size={14} />,
  },
  deploying: {
    dot: "bg-emerald-400",
    pill: "border-emerald-200 bg-emerald-50 text-emerald-700",
    icon: <Rocket size={14} />,
  },
  completed: {
    dot: "bg-emerald-500",
    pill: "border-emerald-200 bg-emerald-50 text-emerald-700",
    icon: <CheckCircle2 size={14} />,
  },
  failed: {
    dot: "bg-red-500",
    pill: "border-red-200 bg-red-50 text-red-700",
    icon: <XCircle size={14} />,
  },
};

export default function AgentStatusBar({ phase }: { phase: AgentPhase }) {
  const style = PHASE_STYLE[phase] ?? PHASE_STYLE.running_commands;
  const label = AGENT_PHASE_LABELS[phase] ?? phase;
  const isWaiting = phase.startsWith("waiting_for");
  return (
    <div
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-semibold ${style.pill}`}
      role="status"
      aria-live="polite"
    >
      <span className={`h-2 w-2 rounded-full ${style.dot} ${isWaiting ? "" : "animate-pulse"}`} />
      {style.icon}
      <span>{label}</span>
    </div>
  );
}
