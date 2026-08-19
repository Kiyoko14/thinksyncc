"use client";

import { CheckCircle2, XCircle, Loader2 } from "lucide-react";
import type { StepResult } from "@/services/api";
import { humanizeStep } from "@/services/api";

function formatDuration(ms: number): string {
  if (!ms || ms < 0) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Vertical workflow: one active step highlighted on top, completed steps below.
 * No debug/engineering detail — just title, success/failure, duration.
 */
export default function StepTimeline({
  activeLabel,
  isActive,
  steps,
}: {
  activeLabel: string | null;
  isActive: boolean;
  steps: StepResult[];
}) {
  return (
    <div className="app-panel p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Progress</p>
          <p className="mt-1 text-sm text-slate-600">High-level progress first, step detail second.</p>
        </div>
        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">{steps.length} steps</span>
      </div>

      {isActive && activeLabel ? (
        <div className="mb-3 flex items-center gap-2 rounded-2xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm font-medium text-emerald-900">
          <Loader2 size={16} className="animate-spin flex-shrink-0" />
          <span className="truncate">{activeLabel}</span>
        </div>
      ) : null}

      {steps.length === 0 && !isActive ? (
        <p className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-3 py-4 text-sm text-slate-500">
          ThinkSync has not started executing this workspace yet.
        </p>
      ) : (
        <ol className="space-y-2">
          {steps.map((step) => (
            <li key={step.step} className="rounded-2xl border border-slate-200 bg-white px-3 py-3 text-sm">
              <div className="flex items-start gap-3">
                {step.success ? (
                  <CheckCircle2 size={16} className="mt-0.5 flex-shrink-0 text-emerald-600" />
                ) : (
                  <XCircle size={16} className="mt-0.5 flex-shrink-0 text-red-500" />
                )}
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium text-slate-900">{humanizeStep(step.tool, step.args)}</p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {step.success ? "Completed" : "Failed"} • {formatDuration(step.duration_ms)}
                  </p>
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
