"use client";

import { useEffect, useState, useRef } from "react";
import { useRouter } from "next/navigation";
import BottomNav from "@/components/BottomNav";
import {
  ApiError,
  getServers,
  addServer,
  deleteServer,
} from "@/services/api";
import { getToken, logout } from "@/services/auth";
import type { Server, ServerCreatePayload } from "@/services/api";

const EMPTY_FORM: ServerCreatePayload = {
  name: "",
  host: "",
  ssh_user: "root",
  ssh_port: 22,
  ssh_auth_method: "password",
  ssh_password: "",
  ssh_key: "",
};

export default function ServersPage() {
  const router = useRouter();
  const [servers, setServers] = useState<Server[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [showSheet, setShowSheet] = useState(false);
  const [form, setForm] = useState<ServerCreatePayload>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [openingId, setOpeningId] = useState<string | null>(null);
  const sheetRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    void loadServers();
  }, [router]);

  // Close sheet when tapping backdrop
  useEffect(() => {
    if (!showSheet) return;
    const handler = (e: MouseEvent) => {
      if (sheetRef.current && !sheetRef.current.contains(e.target as Node)) {
        setShowSheet(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showSheet]);

  const loadServers = async () => {
    setLoadError("");
    try {
      const data = await getServers();
      setServers(data);
    } catch (err: unknown) {
      setLoadError(err instanceof Error ? err.message : "Failed to load servers");
    } finally {
      setLoading(false);
    }
  };

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setFormError("");
    try {
      await addServer(form);
      setShowSheet(false);
      setForm(EMPTY_FORM);
      await loadServers();
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : "Failed to add server");
    } finally {
      setSubmitting(false);
    }
  };

  const handleServerTap = async (server: Server) => {
    const serverId = server?.id;
    if (!serverId) {
      console.error("Cannot open workspace: server_id is missing", server);
      return;
    }

    setOpeningId(serverId);
    try {
      router.push(`/servers/${serverId}/workspaces`);
    } catch (err: unknown) {
      console.error("Failed to open workspace", err);

      if (err instanceof ApiError && err.status === 401) {
        logout();
        router.replace("/login");
      }
    } finally {
      setOpeningId(null);
    }
  };

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    await deleteServer(id);
    setServers((prev) => prev.filter((s) => s.id !== id));
  };

  const handleLogout = () => {
    logout();
    router.replace("/login");
  };

  return (
    <div className="min-h-screen bg-gray-950 text-white pb-20">
      {/* Header */}
      <header className="sticky top-0 z-30 bg-gray-950/95 backdrop-blur border-b border-gray-800 safe-top">
        <div className="flex items-center justify-between px-4 h-14">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="4 17 10 11 4 5" />
                <line x1="12" y1="19" x2="20" y2="19" />
              </svg>
            </div>
            <span className="font-bold text-white text-base tracking-tight">ThinkSync</span>
          </div>
          <button
            onClick={handleLogout}
            className="text-gray-500 hover:text-gray-300 transition-colors p-1"
            aria-label="Logout"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <polyline points="16 17 21 12 16 7" />
              <line x1="21" y1="12" x2="9" y2="12" />
            </svg>
          </button>
        </div>
      </header>

      {/* Page title */}
      <div className="px-4 pt-5 pb-3">
        <h1 className="text-2xl font-bold text-white">Servers</h1>
        <p className="text-gray-500 text-sm mt-0.5">
          {loading ? "Loading…" : `${servers.length} server${servers.length !== 1 ? "s" : ""}`}
        </p>
      </div>

      {/* Server list */}
      <main className="px-4">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <svg className="animate-spin h-6 w-6 text-blue-500" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
            </svg>
          </div>
        ) : loadError ? (
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-5">
            <p className="text-sm font-semibold text-white">Couldn&apos;t load servers</p>
            <p className="text-sm text-gray-500 mt-1">{loadError}</p>
            <button
              onClick={() => {
                setLoading(true);
                void loadServers();
              }}
              className="mt-4 bg-gray-800 hover:bg-gray-700 text-white text-sm font-medium px-4 py-2.5 rounded-xl transition-colors"
            >
              Retry
            </button>
          </div>
        ) : servers.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-center">
            <div className="w-16 h-16 rounded-2xl bg-gray-800 flex items-center justify-center mb-4">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#4b5563" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <rect x="2" y="2" width="20" height="8" rx="2" />
                <rect x="2" y="14" width="20" height="8" rx="2" />
              </svg>
            </div>
            <p className="text-gray-400 font-medium">No servers yet</p>
            <p className="text-gray-600 text-sm mt-1">Tap + to add your first server</p>
          </div>
        ) : (
          <div className="space-y-3">
            {servers.map((server) => (
              <button
                key={server.id}
                onClick={() => handleServerTap(server)}
                disabled={openingId === server.id}
                className="w-full text-left bg-gray-900 border border-gray-800 rounded-2xl px-4 py-4 active:scale-[0.98] transition-transform disabled:opacity-60"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="w-10 h-10 rounded-xl bg-gray-800 flex items-center justify-center flex-shrink-0">
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                        <rect x="2" y="2" width="20" height="8" rx="2" />
                        <rect x="2" y="14" width="20" height="8" rx="2" />
                        <circle cx="6" cy="6" r="1" fill="#60a5fa" stroke="none" />
                        <circle cx="6" cy="18" r="1" fill="#60a5fa" stroke="none" />
                      </svg>
                    </div>
                    <div className="min-w-0">
                      <p className="font-semibold text-white truncate">{server.name}</p>
                      <p className="text-xs text-gray-500 mt-0.5 truncate font-mono">
                        {server.ssh_user}@{server.host}:{server.ssh_port}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0 ml-2">
                    {openingId === server.id ? (
                      <svg className="animate-spin h-4 w-4 text-blue-400" viewBox="0 0 24 24" fill="none">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                      </svg>
                    ) : (
                      <>
                        <button
                          onClick={(e) => handleDelete(e, server.id)}
                          className="text-gray-600 hover:text-red-400 transition-colors p-1"
                          aria-label="Delete server"
                        >
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                            <polyline points="3 6 5 6 21 6" />
                            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                            <path d="M10 11v6M14 11v6" />
                          </svg>
                        </button>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4b5563" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="9 18 15 12 9 6" />
                        </svg>
                      </>
                    )}
                  </div>
                </div>
                <div className="mt-2 ml-13 flex items-center gap-2 pl-[52px]">
                  <span className="inline-flex items-center gap-1 text-[11px] bg-gray-800 text-gray-400 px-2 py-0.5 rounded-full">
                    <span className="w-1.5 h-1.5 rounded-full bg-green-400 inline-block" />
                    {server.ssh_auth_method === "password" ? "Password" : "SSH Key"}
                  </span>
                </div>
              </button>
            ))}
          </div>
        )}
      </main>

      {/* FAB — Add Server */}
      <button
        onClick={() => { setShowSheet(true); setFormError(""); }}
        className="fixed bottom-20 right-5 z-30 w-14 h-14 bg-blue-600 hover:bg-blue-500 active:bg-blue-700 rounded-full shadow-lg shadow-blue-900/50 flex items-center justify-center transition-colors"
        aria-label="Add server"
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round">
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      </button>

      {/* Bottom sheet backdrop */}
      {showSheet && (
        <div className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm flex items-end">
          <div
            ref={sheetRef}
            className="w-full bg-gray-900 rounded-t-3xl border-t border-gray-800 max-h-[90vh] overflow-y-auto"
          >
            {/* Sheet handle */}
            <div className="flex justify-center pt-3 pb-1">
              <div className="w-10 h-1 bg-gray-700 rounded-full" />
            </div>

            <div className="px-5 pb-8 pt-3">
              <div className="flex items-center justify-between mb-5">
                <h2 className="text-lg font-bold text-white">Add Server</h2>
                <button
                  onClick={() => setShowSheet(false)}
                  className="text-gray-500 hover:text-white transition-colors p-1"
                >
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                    <line x1="18" y1="6" x2="6" y2="18" />
                    <line x1="6" y1="6" x2="18" y2="18" />
                  </svg>
                </button>
              </div>

              <form onSubmit={handleAdd} className="space-y-4">
                <div className="grid grid-cols-2 gap-3">
                  <div className="col-span-2">
                    <label className="block text-xs font-medium text-gray-400 mb-1.5 uppercase tracking-wide">Name</label>
                    <input
                      className="w-full bg-gray-800 text-white rounded-xl px-4 py-3 border border-gray-700 focus:outline-none focus:border-blue-500 text-base"
                      placeholder="production-1"
                      value={form.name}
                      onChange={(e) => setForm({ ...form, name: e.target.value })}
                      required
                    />
                  </div>
                  <div className="col-span-2">
                    <label className="block text-xs font-medium text-gray-400 mb-1.5 uppercase tracking-wide">Host / IP</label>
                    <input
                      className="w-full bg-gray-800 text-white rounded-xl px-4 py-3 border border-gray-700 focus:outline-none focus:border-blue-500 text-base font-mono"
                      placeholder="192.168.1.1"
                      value={form.host}
                      onChange={(e) => setForm({ ...form, host: e.target.value })}
                      required
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1.5 uppercase tracking-wide">SSH User</label>
                    <input
                      className="w-full bg-gray-800 text-white rounded-xl px-4 py-3 border border-gray-700 focus:outline-none focus:border-blue-500 text-base"
                      value={form.ssh_user}
                      onChange={(e) => setForm({ ...form, ssh_user: e.target.value })}
                      required
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1.5 uppercase tracking-wide">Port</label>
                    <input
                      type="number"
                      min={1}
                      max={65535}
                      className="w-full bg-gray-800 text-white rounded-xl px-4 py-3 border border-gray-700 focus:outline-none focus:border-blue-500 text-base"
                      value={form.ssh_port}
                      onChange={(e) => setForm({ ...form, ssh_port: Number(e.target.value) })}
                      required
                    />
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-medium text-gray-400 mb-1.5 uppercase tracking-wide">Auth Method</label>
                  <div className="flex gap-2">
                    {(["password", "key"] as const).map((method) => (
                      <button
                        key={method}
                        type="button"
                        onClick={() => setForm({ ...form, ssh_auth_method: method })}
                        className={`flex-1 py-2.5 rounded-xl border text-sm font-medium transition-colors ${
                          form.ssh_auth_method === method
                            ? "bg-blue-600 border-blue-600 text-white"
                            : "bg-gray-800 border-gray-700 text-gray-400"
                        }`}
                      >
                        {method === "password" ? "Password" : "SSH Key"}
                      </button>
                    ))}
                  </div>
                </div>

                {form.ssh_auth_method === "password" && (
                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1.5 uppercase tracking-wide">SSH Password</label>
                    <input
                      type="password"
                      autoComplete="new-password"
                      className="w-full bg-gray-800 text-white rounded-xl px-4 py-3 border border-gray-700 focus:outline-none focus:border-blue-500 text-base"
                      value={form.ssh_password ?? ""}
                      onChange={(e) => setForm({ ...form, ssh_password: e.target.value })}
                    />
                  </div>
                )}

                {form.ssh_auth_method === "key" && (
                  <div>
                    <label className="block text-xs font-medium text-gray-400 mb-1.5 uppercase tracking-wide">Private Key</label>
                    <textarea
                      rows={5}
                      className="w-full bg-gray-800 text-white rounded-xl px-4 py-3 border border-gray-700 font-mono text-xs focus:outline-none focus:border-blue-500"
                      value={form.ssh_key ?? ""}
                      onChange={(e) => setForm({ ...form, ssh_key: e.target.value })}
                      placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                    />
                  </div>
                )}

                {formError && (
                  <div className="bg-red-950/60 border border-red-800 rounded-xl px-4 py-3">
                    <p className="text-red-400 text-sm">{formError}</p>
                  </div>
                )}

                <button
                  type="submit"
                  disabled={submitting}
                  className="w-full bg-blue-600 hover:bg-blue-500 active:bg-blue-700 text-white font-semibold py-3.5 rounded-xl transition-colors disabled:opacity-50 text-base"
                >
                  {submitting ? (
                    <span className="flex items-center justify-center gap-2">
                      <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                      </svg>
                      Saving…
                    </span>
                  ) : (
                    "Add Server"
                  )}
                </button>
              </form>
            </div>
          </div>
        </div>
      )}

      <BottomNav />
    </div>
  );
}
