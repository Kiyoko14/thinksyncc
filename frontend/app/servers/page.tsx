"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import BottomNav from "@/components/BottomNav";
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
  const [workspaceCountById, setWorkspaceCountById] = useState<Record<string, number>>({});
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

    const hydrateServer = async (server: Server) => {
      try {
        const workspaces = await withTimeout(getWorkspacesByServer(server.id), 1800);
        if (!cancelled) {
          setStatusById((prev) => ({ ...prev, [server.id]: "online" }));
          setWorkspaceCountById((prev) => ({ ...prev, [server.id]: workspaces.length }));
        }
      } catch {
        if (!cancelled) {
          setStatusById((prev) => ({ ...prev, [server.id]: "offline" }));
          setWorkspaceCountById((prev) => ({ ...prev, [server.id]: 0 }));
        }
      }
    };

    servers.forEach((server) => {
      if (statusById[server.id] === undefined) {
        void hydrateServer(server);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [servers, statusById]);

  const serverCountLabel = useMemo(() => {
    if (loading) return "Loading inventory…";
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
    <div className="min-h-screen bg-transparent text-slate-900 safe-top pb-24">
      <header className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-4 sm:px-6 lg:px-8">
          <div className="min-w-0">
            <div className="inline-flex rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-600">
              Infrastructure
            </div>
            <h1 className="mt-3 text-2xl font-semibold tracking-tight text-slate-950">Servers</h1>
            <p className="mt-1 text-sm text-slate-600">{serverCountLabel}</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => { setShowAdd(true); setFormError(null); }} className="app-button-accent">
              + Add server
            </button>
            <button
              onClick={() => {
                logout();
                router.replace("/login");
              }}
              className="app-button-secondary"
            >
              Logout
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <section className="app-surface overflow-hidden">
          <div className="grid gap-0 lg:grid-cols-[1.1fr_0.9fr]">
            <div className="subtle-grid border-b border-slate-200/70 px-6 py-6 lg:border-b-0 lg:border-r lg:px-8 lg:py-8">
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Server management</p>
              <h2 className="mt-4 max-w-2xl text-3xl font-semibold tracking-tight text-slate-950">
                Every server should clearly show where ThinkSync will work.
              </h2>
              <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">
                Use the host, SSH identity, status, and workspace count to understand what is connected before you open a workspace.
              </p>
            </div>
            <div className="px-6 py-6 lg:px-8 lg:py-8">
              <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-1">
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Connected</p>
                  <p className="mt-2 text-2xl font-semibold text-slate-950">{servers.length}</p>
                </div>
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Status</p>
                  <p className="mt-2 text-sm font-semibold text-slate-900">{loading ? "Checking reachability" : "Available"}</p>
                </div>
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Action</p>
                  <p className="mt-2 text-sm font-semibold text-slate-900">Open a server or add a new one</p>
                </div>
              </div>
            </div>
          </div>
        </section>

        {error ? (
          <div className="mt-6 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
        ) : null}

        <section className="mt-6">
          {loading ? (
            <div className="app-panel px-4 py-12 text-center text-sm text-slate-500">Loading servers…</div>
          ) : servers.length === 0 ? (
            <div className="app-panel px-6 py-10 text-center">
              <p className="text-sm font-semibold text-slate-900">No servers yet</p>
              <p className="mt-1 text-sm text-slate-500">Connect your first server to give ThinkSync an environment to work in.</p>
              <button onClick={() => setShowAdd(true)} className="app-button-accent mt-4">
                + Add server
              </button>
            </div>
          ) : (
            <ServerList servers={servers} statusById={statusById} workspaceCountById={workspaceCountById} onSelect={onSelect} />
          )}
        </section>
      </main>

      {showAdd ? (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/55 p-4 backdrop-blur-sm sm:items-center">
          <div className="w-full max-w-2xl app-surface p-6">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-slate-900">Add server</p>
                <p className="mt-1 text-sm text-slate-500">Connect a host so ThinkSync has somewhere to work.</p>
              </div>
              <button onClick={() => setShowAdd(false)} className="app-button-secondary px-3 py-2" aria-label="Close">
                ×
              </button>
            </div>

            <form onSubmit={onSubmit} className="mt-5 grid gap-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <label className="mb-2 block text-sm font-medium text-slate-700">Name</label>
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="app-input"
                    placeholder="production"
                    required
                  />
                </div>
                <div className="sm:col-span-2">
                  <label className="mb-2 block text-sm font-medium text-slate-700">Host</label>
                  <input
                    value={form.host}
                    onChange={(e) => setForm({ ...form, host: e.target.value })}
                    className="app-input app-mono"
                    placeholder="203.0.113.10"
                    required
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-medium text-slate-700">SSH user</label>
                  <input
                    value={form.ssh_user}
                    onChange={(e) => setForm({ ...form, ssh_user: e.target.value })}
                    className="app-input"
                    required
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-medium text-slate-700">Port</label>
                  <input
                    type="number"
                    min={1}
                    max={65535}
                    value={form.ssh_port}
                    onChange={(e) => setForm({ ...form, ssh_port: Number(e.target.value) })}
                    className="app-input"
                    required
                  />
                </div>
              </div>

              <div>
                <label className="mb-2 block text-sm font-medium text-slate-700">Auth method</label>
                <div className="inline-flex rounded-2xl border border-slate-200 bg-slate-50 p-1">
                  {(["password", "key"] as const).map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => setForm({ ...form, ssh_auth_method: m })}
                      className={`rounded-xl px-3 py-2 text-sm font-semibold transition ${
                        form.ssh_auth_method === m ? "bg-white text-slate-950 shadow-sm" : "text-slate-500 hover:text-slate-900"
                      }`}
                    >
                      {m === "password" ? "Password" : "SSH Key"}
                    </button>
                  ))}
                </div>
              </div>

              {form.ssh_auth_method === "password" ? (
                <div>
                  <label className="mb-2 block text-sm font-medium text-slate-700">SSH password</label>
                  <input
                    type="password"
                    value={form.ssh_password ?? ""}
                    onChange={(e) => setForm({ ...form, ssh_password: e.target.value })}
                    className="app-input"
                  />
                </div>
              ) : (
                <div>
                  <label className="mb-2 block text-sm font-medium text-slate-700">Private key</label>
                  <textarea
                    rows={6}
                    value={form.ssh_key ?? ""}
                    onChange={(e) => setForm({ ...form, ssh_key: e.target.value })}
                    className="app-textarea app-mono"
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                  />
                </div>
              )}

              {formError ? <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{formError}</div> : null}

              <div className="flex gap-3 pt-1">
                <button type="button" onClick={() => setShowAdd(false)} className="app-button-secondary flex-1">
                  Cancel
                </button>
                <button type="submit" disabled={submitting} className="app-button-accent flex-1">
                  {submitting ? "Saving…" : "Save server"}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}

      <BottomNav />
    </div>
  );
}
