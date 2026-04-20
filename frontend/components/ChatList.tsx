"use client";

import type { Workspace } from "@/services/api";

export default function ChatList({
  chats,
  activeChatId,
  onSelect,
  onNewChat,
  creating,
}: {
  chats: Workspace[];
  activeChatId: string | null;
  onSelect: (chat: Workspace) => void;
  onNewChat: () => void;
  creating: boolean;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-gray-800 px-3 py-3">
        <p className="text-sm font-semibold text-white">Chats</p>
        <button
          onClick={onNewChat}
          disabled={creating}
          className="rounded-xl bg-gray-800 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-gray-700 disabled:opacity-60"
        >
          + New Chat
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {chats.length === 0 ? (
          <div className="p-3 text-sm text-gray-500">No chats yet.</div>
        ) : (
          <div className="space-y-1">
            {chats.map((chat) => {
              const active = chat.id === activeChatId;
              return (
                <button
                  key={chat.id}
                  onClick={() => onSelect(chat)}
                  className={`w-full rounded-xl px-3 py-2 text-left transition ${
                    active
                      ? "bg-blue-600/15 text-white ring-1 ring-blue-500/30"
                      : "bg-transparent text-gray-300 hover:bg-gray-900/60 hover:text-white"
                  }`}
                >
                  <p className="truncate text-sm font-medium">{chat.name}</p>
                  <p className="mt-0.5 truncate text-[11px] text-gray-500">{chat.slug}</p>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
