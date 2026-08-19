"use client";

import { useEffect, useRef } from "react";

export default function LogsPanel({
  title,
  logs,
}: {
  title: string;
  logs: string;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [logs.length]);

  return (
    <details className="app-panel overflow-hidden">
      <summary className="cursor-pointer list-none border-b border-slate-200 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold text-slate-900">{title}</p>
            <p className="mt-1 text-xs text-slate-500">Expandable command output</p>
          </div>
          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">Logs</span>
        </div>
      </summary>
      <div className="max-h-[420px] overflow-y-auto px-4 py-4">
        {logs.trim().length === 0 ? (
          <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-3 py-4 text-sm text-slate-500">
            No logs captured yet.
          </div>
        ) : (
          <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-2xl border border-slate-200 bg-slate-950 px-3 py-3 font-mono text-xs leading-relaxed text-slate-100">
            {logs}
          </pre>
        )}
        <div ref={bottomRef} />
      </div>
    </details>
  );
}
