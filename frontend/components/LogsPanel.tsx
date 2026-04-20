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
    <div className="flex h-full flex-col">
      <div className="border-b border-gray-800 px-4 py-3">
        <p className="text-sm font-semibold text-white">{title}</p>
        <p className="mt-1 text-xs text-gray-500">Live tool output</p>
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {logs.trim().length === 0 ? (
          <div className="text-sm text-gray-500">No logs yet.</div>
        ) : (
          <pre className="whitespace-pre-wrap break-words rounded-2xl border border-gray-800 bg-gray-950/30 p-3 font-mono text-xs leading-relaxed text-gray-100">
            {logs}
          </pre>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

