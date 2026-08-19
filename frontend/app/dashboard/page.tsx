"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import BottomNav from "@/components/BottomNav";
import { getServers, getWorkspacesByServer, type Server } from "@/services/api";
import { getToken, logout } from "@/services/auth";

type DashboardServer = Server & { workspaceCount?: number };

export default function DashboardPage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [servers, setServers] = useState<DashboardServer[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const list = await getServers();
        if (cancelled) return;
        setServers(list.map((server) => ({ ...server, workspaceCount: 0 })));
        const counts = await Promise.all(
          list.map(async (server) => {
            try {
              const workspaces = await getWorkspacesByServer(server.id);
              return { id: server.id, count: workspaces.length };
            } catch {
              return { id: server.id, count: 0 };
            }
          }),
        );
        if (cancelled) return;
        setServers(
          list.map((server) => ({
            ...server,
            workspaceCount: counts.find((item) => item.id === server.id)?.count ?? 0,
          })),
        );
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load dashboard.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
          setReady(true);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [router]);

  const serverCountLabel = useMemo(() => {
    if (loading) return "Loading infrastructure…";
    return `${servers.length} server${servers.length === 1 ? "" : "s"}`;
  }, [loading, servers.length]);

  if (!ready) return null;

  const recentServers = servers.slice(0, 4);

  return (
    <div className="min-h-screen bg-transparent text-slate-900 safe-top pb-24">
      <header className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-4 sm:px-6 lg:px-8">
          <div className="min-w-0">
            <div className="inline-flex rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-emerald-700">
              ThinkSync
            </div>
            <h1 className="mt-3 text-2xl font-semibold tracking-tight text-slate-950 sm:text-3xl">
              AI-powered server operations
            </h1>
            <p className="mt-2 text-sm text-slate-600">
              Connect a server, create a workspace, and let ThinkSync execute the task.
            </p>
          </div>
          <button
            onClick={() => {
              logout();
              router.replace("/login");
            }}
            className="app-button-secondary whitespace-nowrap"
            aria-label="Logout"
          >
            Logout
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <section className="app-surface overflow-hidden">
          <div className="grid gap-0 lg:grid-cols-[1.2fr_0.8fr]">
            <div className="subtle-grid border-b border-slate-200/70 px-6 py-6 lg:border-b-0 lg:border-r lg:px-8 lg:py-8">
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">
                Workspace orchestration
              </p>
              <h2 className="mt-4 max-w-2xl text-3xl font-semibold tracking-tight text-slate-950">
                ThinkSync connects infrastructure, workspaces, execution, and deployment in one flow.
              </h2>
              <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">
                Judges should be able to understand the product in one glance: connect a server, create a workspace, send a task, watch the agent work, and open the result.
              </p>
              <div className="mt-6 flex flex-wrap gap-3">
              <a href="/servers" className="app-button-accent">
                  Open servers
                </a>
                <button onClick={() => router.refresh()} className="app-button-secondary">
                  Refresh overview
                </button>
              </div>
            </div>

            <div className="px-6 py-6 lg:px-8 lg:py-8">
              <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-1">
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Servers</p>
                  <p className="mt-2 text-2xl font-semibold text-slate-950">{serverCountLabel}</p>
                </div>
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">State</p>
                  <p className="mt-2 text-sm font-semibold text-slate-900">
                    {loading ? "Loading inventory" : "Ready for execution"}
                  </p>
                </div>
                <div className="app-panel-subtle p-4">
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Next step</p>
                  <p className="mt-2 text-sm font-semibold text-slate-900">Open a workspace and start chatting</p>
                </div>
              </div>
            </div>
          </div>
        </section>

        {error ? (
          <div className="mt-6 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        ) : null}

        <section className="mt-6 grid gap-6 xl:grid-cols-[1.1fr_0.9fr]">
          <div className="app-panel p-6">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-slate-900">Servers</p>
                <p className="mt-1 text-sm text-slate-500">Real connected infrastructure available to ThinkSync.</p>
              </div>
              <a href="/servers" className="text-sm font-semibold text-emerald-700 hover:text-emerald-800">
                Manage all
              </a>
            </div>
            <div className="mt-4 space-y-3">
              {recentServers.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-5 text-sm text-slate-500">
                  Connect your first server to give ThinkSync an environment to work in.
                </div>
              ) : (
                recentServers.map((server) => (
                  <button
                    key={server.id}
                    onClick={() => router.push(`/server/${server.id}`)}
                    className="group w-full rounded-[24px] border border-slate-200 bg-white px-4 py-4 text-left transition hover:border-emerald-200 hover:bg-emerald-50/40"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <p className="truncate text-base font-semibold text-slate-950">{server.name}</p>
                        <p className="mt-1 truncate font-mono text-xs text-slate-500">
                          {server.ssh_user}@{server.host}:{server.ssh_port}
                        </p>
                      </div>
                      <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs font-semibold text-slate-600">
                        {server.workspaceCount ?? 0} workspace{(server.workspaceCount ?? 0) === 1 ? "" : "s"}
                      </span>
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>

          <div className="app-panel p-6">
            <p className="text-sm font-semibold text-slate-900">Workflow</p>
            <div className="mt-4 space-y-3">
              {[
                "Connect server",
                "Create workspace",
                "Send task",
                "Watch execution",
                "Review result and deployment",
              ].map((item, index) => (
                <div key={item} className="flex items-center gap-3 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3">
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-950 text-xs font-semibold text-white">
                    {index + 1}
                  </div>
                  <p className="text-sm font-medium text-slate-800">{item}</p>
                </div>
              ))}
            </div>
          </div>
        </section>
      </main>

      <BottomNav />
    </div>
  );
}
