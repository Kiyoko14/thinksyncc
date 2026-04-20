"use client";

import type { Server } from "@/services/api";

export type ServerStatus = "online" | "offline";

export default function ServerList({
  servers,
  statusById,
  onSelect,
}: {
  servers: Server[];
  statusById: Record<string, ServerStatus | undefined>;
  onSelect: (server: Server) => void;
}) {
  return (
    <div className="space-y-3">
      {servers.map((server) => {
        const status = statusById[server.id] ?? "offline";
        const isOnline = status === "online";
        return (
          <button
            key={server.id}
            onClick={() => onSelect(server)}
            className="group w-full rounded-2xl border border-gray-800 bg-gray-900/60 px-4 py-4 text-left transition hover:border-gray-700 hover:bg-gray-900"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="truncate text-base font-semibold text-white">{server.name}</p>
                  <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] ${
                    isOnline
                      ? "border-green-900/60 bg-green-950/40 text-green-200"
                      : "border-gray-800 bg-gray-950/20 text-gray-400"
                  }`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${isOnline ? "bg-green-400" : "bg-gray-500"}`} />
                    {isOnline ? "Online" : "Offline"}
                  </span>
                </div>
                <p className="mt-1 truncate font-mono text-xs text-gray-500">
                  {server.ssh_user}@{server.host}:{server.ssh_port}
                </p>
              </div>
              <svg
                className="mt-1 h-4 w-4 flex-shrink-0 text-gray-600 transition group-hover:text-gray-400"
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
          </button>
        );
      })}
    </div>
  );
}

