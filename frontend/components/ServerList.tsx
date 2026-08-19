"use client";

import type { Server } from "@/services/api";

export type ServerStatus = "online" | "offline";

export default function ServerList({
  servers,
  statusById,
  workspaceCountById,
  onSelect,
}: {
  servers: Server[];
  statusById: Record<string, ServerStatus | undefined>;
  workspaceCountById?: Record<string, number | undefined>;
  onSelect: (server: Server) => void;
}) {
  return (
    <div className="grid gap-3">
      {servers.map((server) => {
        const status = statusById[server.id] ?? "offline";
        const isOnline = status === "online";
        const workspaceCount = workspaceCountById?.[server.id];
        return (
          <button
            key={server.id}
            onClick={() => onSelect(server)}
            className="group w-full rounded-[24px] border border-slate-200 bg-white/90 px-4 py-4 text-left transition hover:border-emerald-200 hover:bg-emerald-50/50"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="truncate text-base font-semibold text-slate-900">{server.name}</p>
                  <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] ${
                    isOnline
                      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                      : "border-slate-200 bg-slate-50 text-slate-500"
                  }`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${isOnline ? "bg-emerald-500" : "bg-slate-400"}`} />
                    {isOnline ? "Online" : "Offline"}
                  </span>
                </div>
                <p className="mt-1 truncate font-mono text-xs text-slate-500">
                  {server.ssh_user}@{server.host}:{server.ssh_port}
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <span className="app-chip border-slate-200 bg-slate-50 text-slate-600">
                    {server.ssh_auth_method === "password" ? "Password auth" : "SSH key auth"}
                  </span>
                  {typeof workspaceCount === "number" ? (
                    <span className="app-chip border-slate-200 bg-slate-50 text-slate-600">
                      {workspaceCount} workspace{workspaceCount === 1 ? "" : "s"}
                    </span>
                  ) : null}
                </div>
              </div>
              <svg
                className="mt-1 h-4 w-4 flex-shrink-0 text-slate-400 transition group-hover:text-slate-600"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </div>
            <div className="mt-4 text-sm font-semibold text-slate-700">Open server</div>
          </button>
        );
      })}
    </div>
  );
}
