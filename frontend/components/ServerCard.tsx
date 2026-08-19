"use client";

import type { Server } from "@/services/api";

interface Props {
  server: Server;
  onDelete: (id: string) => void;
}

export default function ServerCard({ server, onDelete }: Props) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4 flex items-center justify-between">
      <div>
        <p className="font-semibold text-white">{server.name}</p>
        <p className="text-sm text-gray-400 mt-0.5">
          {server.ssh_user}@{server.host}:{server.ssh_port}
        </p>
        <span className="mt-2 inline-block text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded">
          {server.ssh_auth_method}
        </span>
      </div>

      <button
        onClick={() => onDelete(server.id)}
        className="text-red-400 hover:text-red-300 text-sm transition"
      >
        Delete
      </button>
    </div>
  );
}
