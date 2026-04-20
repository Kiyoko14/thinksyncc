"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import ServerList, { type ServerStatus } from "@/components/ServerList";
import { addServer, getServers, getWorkspacesByServer, type Server, type ServerCreatePayload } from "@/services/api";
import { getToken, logout } from "@/services/auth";

const EMPTY_FORM: ServerCreatePayload = {
  name: "",
  host: "",
  ssh_user: "root",
  ssh_port: 22,
  ssh_auth_method: "password",
  ssh_password: "",
  ssh_key: "",
};

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return Promise.race<T>([
    promise,
    new Promise<T>((_, reject) => window.setTimeout(() => reject(new Error("timeout")), ms)),
  ]);
}

export default function ServersPage() {
  const router = useRouter();
  const [servers, setServers] = useState<Server[]>([]);
  const [statusById, setStatusById] = useState<Record<string, ServerStatus>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState<ServerCreatePayload>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getServers();
      setServers(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load servers.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!servers.length) return;
    let cancelled = false;
    const check = async (serverId: string) => {
      try {
        await withTimeout(getWorkspacesByServer(serverId), 1800);
        if (!cancelled) setStatusById((p) => ({ ...p, [serverId]: "online" }));
      } catch {
        if (!cancelled) setStatusById((p) => ({ ...p, [serverId]: "offline" }));
      }
    };
    servers.forEach((s) => {
      if (!statusById[s.id]) void check(s.id);
    });
    return () => {
      cancelled = true;
    };
  }, [servers, statusById]);

  const serverCountLabel = useMemo(() => {
    if (loading) return "Loading…";
    return `${servers.length} server${servers.length === 1 ? "" : "s"}`;
  }, [loading, servers.length]);

  const onSelect = (server: Server) => {
    router.push(`/server/${server.id}`);
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setFormError(null);
    try {
      await addServer(form);
      setShowAdd(false);
      setForm(EMPTY_FORM);
      await load();
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : "Failed to add server.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 text-white safe-top safe-bottom">
      <header className="sticky top-0 z-30 border-b border-gray-800 bg-gray-950/95 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-4xl items-center justify-between px-4">
          <div className="min-w-0">
            <p className="truncate text-base font-semibold">Servers</p>
            <p className="truncate text-xs text-gray-500">{serverCountLabel}</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => { setShowAdd(true); setFormError(null); }}
              className="rounded-xl bg-blue-600 px-3 py-2 text-sm font-semibold text-white transition hover:bg-blue-500"
            >
              + Add Server
            </button>
            <button
              onClick={() => { logout(); router.replace("/login"); }}
              className="rounded-xl border border-gray-800 bg-gray-900/40 px-3 py-2 text-sm font-semibold text-gray-200 transition hover:bg-gray-900"
            >
              Logout
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-6">
        {error ? (
          <div className="rounded-2xl border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-200">
            {error}
          </div>
        ) : null}

        {loading ? (
          <div className="flex items-center justify-center py-20">
            <svg className="h-6 w-6 animate-spin text-blue-500" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
            </svg>
          </div>
        ) : servers.length === 0 ? (
          <div className="rounded-3xl border border-gray-800 bg-gray-900/40 p-8 text-center">
            <p className="text-sm font-semibold text-white">No servers yet</p>
            <p className="mt-1 text-sm text-gray-500">Add your first server to start.</p>
            <button
              onClick={() => setShowAdd(true)}
              className="mt-4 rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-500"
            >
              + Add Server
            </button>
          </div>
        ) : (
          <ServerList servers={servers} statusById={statusById} onSelect={onSelect} />
        )}
      </main>

      {showAdd ? (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 p-4 sm:items-center">
          <div className="w-full max-w-lg rounded-3xl border border-gray-800 bg-gray-950 p-5">
            <div className="flex items-center justify-between">
              <p className="text-base font-semibold text-white">Add Server</p>
              <button
                onClick={() => setShowAdd(false)}
                className="rounded-xl p-2 text-gray-400 transition hover:bg-gray-900 hover:text-white"
                aria-label="Close"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>

            <form onSubmit={onSubmit} className="mt-4 space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div className="col-span-2">
                  <label className="mb-1 block text-xs font-medium text-gray-400">Name</label>
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="w-full rounded-2xl border border-gray-800 bg-gray-900/40 px-3 py-2.5 text-sm text-white placeholder:text-gray-600 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                    placeholder="production"
                    required
                  />
                </div>
                <div className="col-span-2">
                  <label className="mb-1 block text-xs font-medium text-gray-400">Host</label>
                  <input
                    value={form.host}
                    onChange={(e) => setForm({ ...form, host: e.target.value })}
                    className="w-full rounded-2xl border border-gray-800 bg-gray-900/40 px-3 py-2.5 font-mono text-sm text-white placeholder:text-gray-600 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                    placeholder="203.0.113.10"
                    required
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-400">SSH User</label>
                  <input
                    value={form.ssh_user}
                    onChange={(e) => setForm({ ...form, ssh_user: e.target.value })}
                    className="w-full rounded-2xl border border-gray-800 bg-gray-900/40 px-3 py-2.5 text-sm text-white focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                    required
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-400">Port</label>
                  <input
                    type="number"
                    min={1}
                    max={65535}
                    value={form.ssh_port}
                    onChange={(e) => setForm({ ...form, ssh_port: Number(e.target.value) })}
                    className="w-full rounded-2xl border border-gray-800 bg-gray-900/40 px-3 py-2.5 text-sm text-white focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                    required
                  />
                </div>
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-gray-400">Auth</label>
                <div className="inline-flex rounded-2xl border border-gray-800 bg-gray-900/40 p-1">
                  {(["password", "key"] as const).map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => setForm({ ...form, ssh_auth_method: m })}
                      className={`rounded-xl px-3 py-2 text-xs font-semibold transition ${
                        form.ssh_auth_method === m ? "bg-gray-800 text-white" : "text-gray-400 hover:text-gray-200"
                      }`}
                    >
                      {m === "password" ? "Password" : "SSH Key"}
                    </button>
                  ))}
                </div>
              </div>

              {form.ssh_auth_method === "password" ? (
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-400">SSH Password</label>
                  <input
                    type="password"
                    value={form.ssh_password ?? ""}
                    onChange={(e) => setForm({ ...form, ssh_password: e.target.value })}
                    className="w-full rounded-2xl border border-gray-800 bg-gray-900/40 px-3 py-2.5 text-sm text-white focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                  />
                </div>
              ) : (
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-400">Private Key</label>
                  <textarea
                    rows={5}
                    value={form.ssh_key ?? ""}
                    onChange={(e) => setForm({ ...form, ssh_key: e.target.value })}
                    className="w-full rounded-2xl border border-gray-800 bg-gray-900/40 px-3 py-2.5 font-mono text-xs text-white focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                  />
                </div>
              )}

              {formError ? (
                <div className="rounded-2xl border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-200">
                  {formError}
                </div>
              ) : null}

              <div className="flex gap-2 pt-1">
                <button
                  type="button"
                  onClick={() => setShowAdd(false)}
                  className="flex-1 rounded-2xl border border-gray-800 bg-gray-900/40 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-gray-900"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting}
                  className="flex-1 rounded-2xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-500 disabled:opacity-60"
                >
                  {submitting ? "Saving…" : "Save"}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
}

