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
      <div className="flex items-center justify-between border-b border-slate-200 px-3 py-3">
        <p className="text-sm font-semibold text-slate-900">Chats</p>
        <button
          onClick={onNewChat}
          disabled={creating}
          className="rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:bg-slate-50 disabled:opacity-60"
        >
          + New Chat
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {chats.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
            No conversations yet. Start by giving ThinkSync a task.
          </div>
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
                      ? "bg-emerald-50 text-slate-900 ring-1 ring-emerald-200"
                      : "bg-transparent text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                  }`}
                >
                  <p className="truncate text-sm font-semibold">{chat.display_name || chat.name}</p>
                  <p className="mt-0.5 truncate text-[11px] text-slate-500">{chat.slug}</p>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
