'use client';

import { useEffect, useMemo, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { ApiError, createWorkspace, getWorkspacesByServer, type Workspace } from '@/services/api';
import { getToken, logout } from '@/services/auth';

export default function ServerPage() {
  const router = useRouter();
  const params = useParams();
  const serverId = (params.id as string) ?? '';

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);

  const title = useMemo(() => 'Server', []);

  const loadWorkspaces = async () => {
    const list = await getWorkspacesByServer(serverId);
    setWorkspaces(list);
  };

  useEffect(() => {
    if (!serverId) {
      router.replace('/servers');
      return;
    }
    if (!getToken()) {
      router.replace('/login');
      return;
    }

    let cancelled = false;

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        await loadWorkspaces();
      } catch (err: unknown) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          logout();
          router.replace('/login');
          return;
        }
        setError(err instanceof Error ? err.message : 'Failed to load workspaces');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId]);

  const handleNewWorkspace = async () => {
    if (creatingWorkspace) return;

    const name = window.prompt('Enter a name for the new workspace:');
    if (!name || name.trim().length === 0) {
      return; // Abort if user cancels or enters an empty name
    }

    setCreatingWorkspace(true);
    setError(null);
    try {
      const ws = await createWorkspace(serverId, name.trim());
      setWorkspaces((prev) => [ws, ...prev.filter((w) => w.id !== ws.id)]);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 401) {
        logout();
        router.replace('/login');
        return;
      }
      setError(err instanceof Error ? err.message : 'Failed to create workspace');
    } finally {
      setCreatingWorkspace(false);
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-950 text-white">
        <svg className="h-6 w-6 animate-spin text-blue-500" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
        </svg>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white safe-top safe-bottom">
      <header className="flex h-14 items-center justify-between border-b border-gray-800 bg-gray-950/95 px-4 backdrop-blur">
        <button
          onClick={() => router.push('/servers')}
          className="rounded-xl p-2 text-gray-400 transition hover:bg-gray-900/60 hover:text-white"
          aria-label="Back to servers"
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="15 18 9 12 15 6" />
          </svg>
        </button>
        <div className="min-w-0 text-center">
          <p className="truncate text-sm font-semibold">{title}</p>
          <p className="truncate text-[11px] text-gray-500">{serverId}</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => void handleNewWorkspace()}
            disabled={creatingWorkspace}
            className="rounded-xl bg-blue-600 px-3 py-2 text-sm font-semibold text-white transition hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-60"
          >
            + New Workspace
          </button>
          <button
            onClick={() => {
              logout();
              router.replace('/login');
            }}
            className="rounded-xl border border-gray-800 bg-gray-900/40 px-3 py-2 text-sm font-semibold text-gray-200 transition hover:bg-gray-900"
          >
            Logout
          </button>
        </div>
      </header>

      {error ? (
        <div className="mx-auto max-w-4xl px-4 pt-4">
          <div className="rounded-2xl border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-200">
            {error}
          </div>
        </div>
      ) : null}

      <main className="mx-auto max-w-4xl px-4 py-6">
        <div className="rounded-3xl border border-gray-800 bg-gray-900/40 p-5 backdrop-blur">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-white">Workspaces</p>
              <p className="truncate text-xs text-gray-500">{workspaces.length} total</p>
            </div>
          </div>

          {workspaces.length === 0 ? (
            <div className="mt-5 rounded-2xl border border-gray-800 bg-gray-950/20 p-6 text-center">
              <p className="text-sm font-semibold text-white">No workspaces yet</p>
              <p className="mt-1 text-sm text-gray-500">Create your first workspace to start chatting.</p>
              <button
                onClick={() => void handleNewWorkspace()}
                disabled={creatingWorkspace}
                className="mt-4 rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-60"
              >
                + New Workspace
              </button>
            </div>
          ) : (
            <div className="mt-4 divide-y divide-gray-800 overflow-hidden rounded-2xl border border-gray-800">
              {workspaces.map((ws) => (
                <div key={ws.id} className="flex items-center justify-between gap-3 bg-gray-950/20 px-4 py-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-white">{ws.name}</p>
                    <p className="truncate text-xs text-gray-500">{ws.slug}</p>
                  </div>
                  <button
                    onClick={() => router.push(`/chat/${ws.id}`)}
                    className="rounded-xl bg-gray-800 px-3 py-2 text-sm font-semibold text-white transition hover:bg-gray-700"
                  >
                    Open
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
