"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import BottomNav from "@/components/BottomNav";
import { ApiError, createWorkspace, getServers, getWorkspacesByServer } from "@/services/api";
import { getToken, logout } from "@/services/auth";
import type { Server, Workspace } from "@/services/api";

export default function ServerWorkspacesPage() {
  const router = useRouter();
  const params = useParams();
  const serverId = (params.serverId as string) ?? "";

  const [servers, setServers] = useState<Server[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [showModal, setShowModal] = useState(false);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const modalRef = useRef<HTMLDivElement>(null);

  const server = useMemo(() => servers.find((s) => s.id === serverId) ?? null, [servers, serverId]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    if (!serverId) {
      router.replace("/servers");
      return;
    }
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router, serverId]);

  useEffect(() => {
    if (!showModal) return;
    const handler = (e: MouseEvent) => {
      if (modalRef.current && !modalRef.current.contains(e.target as Node)) {
        setShowModal(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showModal]);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [serversResp, workspacesResp] = await Promise.all([
        getServers(),
        getWorkspacesByServer(serverId),
      ]);
      setServers(serversResp);
      setWorkspaces(workspacesResp);
      try {
        workspacesResp.forEach((ws) => {
          localStorage.setItem(`thinksync_workspace_server:${ws.id}`, serverId);
        });
      } catch {
        // ignore storage failures
      }
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 401) {
        logout();
        router.replace("/login");
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load workspaces");
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!serverId) return;
    setSubmitting(true);
    setFormError("");
    try {
      const ws = await createWorkspace(serverId, name);
      try {
        localStorage.setItem(`thinksync_workspace_server:${ws.id}`, serverId);
      } catch {
        // ignore storage failures
      }
      setShowModal(false);
      setName("");
      router.push(`/workspace/${ws.id}/chat`);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 401) {
        logout();
        router.replace("/login");
        return;
      }
      setFormError(err instanceof Error ? err.message : "Failed to create workspace");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 text-white pb-20">
      <header className="sticky top-0 z-30 bg-gray-950/95 backdrop-blur border-b border-gray-800 safe-top">
        <div className="flex items-center justify-between px-4 h-14">
          <button
            onClick={() => router.push("/servers")}
            className="text-gray-400 hover:text-gray-200 transition-colors p-1"
            aria-label="Back"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="15 18 9 12 15 6" />
            </svg>
          </button>
          <div className="flex flex-col items-center">
            <span className="font-semibold text-white text-sm">Workspaces</span>
            <span className="text-[11px] text-gray-500">{server?.name ?? serverId}</span>
          </div>
          <button
            onClick={() => setShowModal(true)}
            className="bg-blue-600 hover:bg-blue-500 active:bg-blue-600 transition-colors text-white text-sm font-semibold px-3 py-1.5 rounded-xl"
          >
            Create
          </button>
        </div>
      </header>

      <main className="px-4 pt-5">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <svg className="animate-spin h-6 w-6 text-blue-500" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
            </svg>
          </div>
        ) : error ? (
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-5">
            <p className="text-sm font-semibold text-white">Couldn&apos;t load workspaces</p>
            <p className="text-sm text-gray-500 mt-1">{error}</p>
            <button
              onClick={() => void load()}
              className="mt-4 bg-gray-800 hover:bg-gray-700 text-white text-sm font-medium px-4 py-2.5 rounded-xl transition-colors"
            >
              Retry
            </button>
          </div>
        ) : workspaces.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-center">
            <div className="w-16 h-16 rounded-2xl bg-gray-800 flex items-center justify-center mb-4">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#4b5563" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 19a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-4l-2-2H6a2 2 0 0 0-2 2z" />
              </svg>
            </div>
            <p className="text-gray-400 font-medium">No workspaces yet</p>
            <p className="text-gray-600 text-sm mt-1">Create a workspace to start a project</p>
            <button
              onClick={() => setShowModal(true)}
              className="mt-5 bg-blue-600 hover:bg-blue-500 text-white text-sm font-semibold px-4 py-2.5 rounded-xl transition-colors"
            >
              Create Workspace
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            {workspaces.map((ws) => (
              <div
                key={ws.id}
                className="bg-gray-900 border border-gray-800 rounded-2xl p-5 flex items-center justify-between"
              >
                <div>
                  <p className="font-semibold text-white">{ws.name}</p>
                  <p className="text-sm text-gray-500 mt-1">{new Date(ws.created_at).toLocaleString()}</p>
                </div>
                <button
                  onClick={() => router.push(`/workspace/${ws.id}/chat`)}
                  className="bg-gray-800 hover:bg-gray-700 text-white text-sm font-semibold px-4 py-2 rounded-xl transition-colors"
                >
                  Open
                </button>
              </div>
            ))}
          </div>
        )}
      </main>

      {showModal && (
        <div className="fixed inset-0 z-50 bg-black/60 flex items-end sm:items-center justify-center p-4">
          <div ref={modalRef} className="w-full max-w-md bg-gray-900 border border-gray-800 rounded-2xl p-5">
            <h2 className="text-lg font-bold text-white">Create Workspace</h2>
            <p className="text-sm text-gray-500 mt-1">A workspace is a project folder on the server.</p>

            <form onSubmit={handleCreate} className="mt-4 space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-400 mb-1">Name</label>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="w-full bg-gray-950 border border-gray-800 rounded-xl px-3 py-2 text-sm text-white placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-600/50"
                  placeholder="e.g. my-next-app"
                  maxLength={150}
                  required
                />
              </div>

              {formError ? (
                <div className="bg-red-950/40 border border-red-900/60 text-red-300 text-sm rounded-xl p-3">
                  {formError}
                </div>
              ) : null}

              <div className="flex gap-2 pt-1">
                <button
                  type="button"
                  onClick={() => setShowModal(false)}
                  className="flex-1 bg-gray-800 hover:bg-gray-700 text-white text-sm font-semibold px-4 py-2.5 rounded-xl transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting}
                  className="flex-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-60 text-white text-sm font-semibold px-4 py-2.5 rounded-xl transition-colors"
                >
                  {submitting ? "Creating…" : "Create"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      <BottomNav />
    </div>
  );
}
