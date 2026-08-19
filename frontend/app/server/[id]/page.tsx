"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import BottomNav from "@/components/BottomNav";
import CreateWorkspaceModal from "@/components/CreateWorkspaceModal";
import { ApiError, getServers, getWorkspacesByServer, type Server, type Workspace } from "@/services/api";
import { getToken, logout } from "@/services/auth";

export default function ServerPage() {
  const router = useRouter();
  const params = useParams();
  const serverId = (params.id as string) ?? "";

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [server, setServer] = useState<Server | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [modalOpen, setModalOpen] = useState(false);

  const load = async () => {
    const [servers, list] = await Promise.all([getServers(), getWorkspacesByServer(serverId)]);
    setServer(servers.find((item) => item.id === serverId) ?? null);
    setWorkspaces(list);
  };

  useEffect(() => {
    if (!serverId) {
      router.replace("/servers");
      return;
    }
    if (!getToken()) {
      router.replace("/login");
      return;
    }

    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        await load();
      } catch (err: unknown) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          logout();
          router.replace("/login");
          return;
        }
        setError(err instanceof Error ? err.message : "Failed to load server.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId]);

  const serverLabel = useMemo(() => server?.name ?? "Server", [server]);

  const handleWorkspaceCreated = (ws: Workspace) => {
    setModalOpen(false);
    setWorkspaces((prev) => [ws, ...prev.filter((item) => item.id !== ws.id)]);
  };

  return (
    <div className="min-h-screen bg-transparent text-slate-900 safe-top safe-bottom">
      <header className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-4 sm:px-6 lg:px-8">
          <button onClick={() => router.push("/servers")} className="app-button-secondary px-3 py-2" aria-label="Back to servers">
            ← Servers
          </button>
          <div className="min-w-0 text-center">
            <p className="truncate text-sm font-semibold text-slate-900">{serverLabel}</p>
            <p className="truncate text-xs text-slate-500">{serverId}</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setModalOpen(true)} className="app-button-accent whitespace-nowrap">
              + New workspace
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
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Server identity</p>
              <h1 className="mt-4 text-3xl font-semibold tracking-tight text-slate-950">
                {server?.name ?? "Loading server"}
              </h1>
              <p className="mt-3 text-sm leading-6 text-slate-600">
                {server ? `${server.ssh_user}@${server.host}:${server.ssh_port}` : "Loading connection details…"}
              </p>
              <div className="mt-5 flex flex-wrap gap-2">
                {server ? (
                  <>
                    <span className="app-chip border-slate-200 bg-slate-50 text-slate-600">
                      {server.ssh_auth_method === "password" ? "Password auth" : "SSH key auth"}
                    </span>
                    <span className="app-chip border-slate-200 bg-slate-50 text-slate-600">
                      {workspaces.length} workspace{workspaces.length === 1 ? "" : "s"}
                    </span>
                  </>
                ) : null}
              </div>
            </div>

            <div className="px-6 py-6 lg:px-8 lg:py-8">
              <div className="space-y-3">
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Connection</p>
                  <p className="mt-2 text-sm font-semibold text-slate-900">{loading ? "Checking server…" : server ? "Connected in inventory" : "Not found"}</p>
                </div>
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Workspaces</p>
                  <p className="mt-2 text-sm font-semibold text-slate-900">{workspaces.length} active on this server</p>
                </div>
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Primary action</p>
                  <p className="mt-2 text-sm font-semibold text-slate-900">Create a workspace and open chat</p>
                </div>
              </div>
            </div>
          </div>
        </section>

        {error ? <div className="mt-6 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div> : null}

        <section className="mt-6 app-panel p-6">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-slate-900">Workspaces</p>
              <p className="mt-1 text-sm text-slate-500">Use display names when available so the workspace is readable in the demo.</p>
            </div>
            <button onClick={() => setModalOpen(true)} className="app-button-secondary">
              + New workspace
            </button>
          </div>

          {loading ? (
            <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-sm text-slate-500">Loading workspaces…</div>
          ) : workspaces.length === 0 ? (
            <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-center">
              <p className="text-sm font-semibold text-slate-900">No workspaces yet</p>
              <p className="mt-1 text-sm text-slate-500">Create a workspace and let ThinkSync start working.</p>
              <button onClick={() => setModalOpen(true)} className="app-button-accent mt-4">
                + New workspace
              </button>
            </div>
          ) : (
            <div className="mt-4 grid gap-3">
              {workspaces.map((workspace) => (
                <div key={workspace.id} className="group rounded-[24px] border border-slate-200 bg-white px-4 py-4 transition hover:border-emerald-200 hover:bg-emerald-50/40">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate text-base font-semibold text-slate-950">
                        {workspace.display_name || workspace.name}
                      </p>
                      <p className="mt-1 truncate text-xs text-slate-500">
                        {workspace.slug} · {workspace.path}
                      </p>
                    </div>
                    <button onClick={() => router.push(`/chat/${workspace.id}`)} className="app-button-primary px-3 py-2">
                      Open chat
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </main>

      {modalOpen ? (
        <CreateWorkspaceModal
          serverId={serverId}
          onCreated={handleWorkspaceCreated}
          onClose={() => setModalOpen(false)}
        />
      ) : null}

      <BottomNav />
    </div>
  );
}
