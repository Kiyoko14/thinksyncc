'use client';

import { useEffect, useState } from 'react';
import {
  createGitHubConnection,
  createWorkspace,
  getGitHubConnections,
  type GitHubConnection,
  type GitHubConnectionCreatePayload,
  type GitHubConnectionWithKey,
  type Workspace,
} from '@/services/api';
import { ApiError } from '@/services/api';

type Mode = 'empty' | 'github';
type GithubSource = 'existing' | 'new';
type NewKeyMode = 'generate' | 'import';

interface Props {
  serverId: string;
  onCreated: (ws: Workspace) => void;
  onClose: () => void;
}

export default function CreateWorkspaceModal({ serverId, onCreated, onClose }: Props) {
  const [name, setName] = useState('');
  const [mode, setMode] = useState<Mode>('empty');
  const [githubSource, setGithubSource] = useState<GithubSource>('existing');
  const [connections, setConnections] = useState<GitHubConnection[]>([]);
  const [selectedConn, setSelectedConn] = useState<string>('');
  const [repo, setRepo] = useState('');
  const [branch, setBranch] = useState('');
  const [depth, setDepth] = useState<number | ''>('');

  // new-connection flow
  const [connName, setConnName] = useState('');
  const [newKeyMode, setNewKeyMode] = useState<NewKeyMode>('generate');
  const [pubKeyInput, setPubKeyInput] = useState('');
  const [privKeyInput, setPrivKeyInput] = useState('');

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generatedKey, setGeneratedKey] = useState<string | null>(null);

  useEffect(() => {
    if (mode !== 'github') return;
    let cancelled = false;
    (async () => {
      try {
        const list = await getGitHubConnections();
        if (cancelled) return;
        setConnections(list);
        if (list.length > 0 && !selectedConn) setSelectedConn(list[0].id);
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load GitHub connections');
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  const handleSubmit = async () => {
    if (loading) return;
    if (!name.trim()) {
      setError('Workspace name is required');
      return;
    }
    if (mode === 'github' && !repo.trim()) {
      setError('Repository (owner/name) is required for GitHub-linked workspaces');
      return;
    }

    setLoading(true);
    setError(null);
    setGeneratedKey(null);

    try {
      let githubConnectionId: string | null = null;

      if (mode === 'github') {
        if (githubSource === 'existing') {
          githubConnectionId = selectedConn || null;
        } else {
          // Create a new connection first.
          const connPayload: GitHubConnectionCreatePayload = {
            name: connName.trim() || `${name.trim()}-gh`,
            auth_method: 'ssh',
          };
          if (newKeyMode === 'generate') {
            connPayload.generate_keypair = true;
          } else {
            if (!pubKeyInput.trim() || !privKeyInput.trim()) {
              setError('Both public and private SSH keys are required when importing');
              setLoading(false);
              return;
            }
            connPayload.ssh_public_key = pubKeyInput.trim();
            connPayload.ssh_private_key = privKeyInput.trim();
          }
          const conn = await createGitHubConnection(connPayload);
          githubConnectionId = conn.id;
          // If the backend generated the key, surface the private key ONCE.
          const withKey = conn as GitHubConnectionWithKey;
          if (newKeyMode === 'generate' && withKey.ssh_private_key) {
            setGeneratedKey(withKey.ssh_private_key);
          }
        }
      }

      const ws = await createWorkspace({
        server_id: serverId,
        name: name.trim(),
        github_connection_id: mode === 'github' ? githubConnectionId : null,
        github_repo: mode === 'github' ? repo.trim() : null,
        github_branch: mode === 'github' && branch.trim() ? branch.trim() : null,
        github_depth:
          mode === 'github' && depth !== '' ? Number(depth) : null,
      });

      onCreated(ws);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 401) {
        // caller handles logout/redirect
        setError('Session expired. Please log in again.');
        return;
      }
      setError(err instanceof Error ? err.message : 'Failed to create workspace');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/55 p-4 backdrop-blur-sm sm:items-center"
      onClick={onClose}
    >
      <div
        className="app-surface w-full max-w-2xl p-6 text-slate-900"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold text-slate-900">New workspace</p>
            <p className="mt-1 text-sm text-slate-500">Create a workspace for this server.</p>
          </div>
          <button
            onClick={onClose}
            className="app-button-secondary px-3 py-2"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {/* Name */}
        <label className="mb-2 block text-sm font-medium text-slate-700">Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="my-project"
          className="app-input mb-4"
        />

        {/* Mode toggle */}
        <div className="mb-4 grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => setMode('empty')}
            className={`rounded-xl border px-3 py-2 text-sm font-medium transition ${
              mode === 'empty'
                ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                : 'border-slate-200 bg-white text-slate-600 hover:text-slate-900'
            }`}
          >
            Empty workspace
          </button>
          <button
            type="button"
            onClick={() => setMode('github')}
            className={`rounded-xl border px-3 py-2 text-sm font-medium transition ${
              mode === 'github'
                ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                : 'border-slate-200 bg-white text-slate-600 hover:text-slate-900'
            }`}
          >
            Link GitHub repo
          </button>
        </div>

        {mode === 'github' && (
          <div className="mb-4 rounded-[24px] border border-slate-200 bg-slate-50/80 p-4">
            <label className="mb-2 block text-sm font-medium text-slate-700">
              Repository (owner/name)
            </label>
            <input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="nous/research"
              className="app-input mb-3"
            />

            <div className="mb-3 grid grid-cols-2 gap-2">
              <div>
                <label className="mb-1 block text-xs text-slate-500">Branch (optional)</label>
                <input
                  value={branch}
                  onChange={(e) => setBranch(e.target.value)}
                  placeholder="main"
                  className="app-input"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-500">
                  Depth (1 = shallow)
                </label>
                <input
                  value={depth}
                  onChange={(e) => setDepth(e.target.value === '' ? '' : Number(e.target.value))}
                  type="number"
                  min={1}
                  max={1}
                  placeholder="full"
                  className="app-input"
                />
              </div>
            </div>

            {/* GitHub connection source */}
            <div className="mb-3 grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setGithubSource('existing')}
                className={`rounded-xl border px-3 py-2 text-xs font-medium transition ${
                  githubSource === 'existing'
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                    : 'border-slate-200 bg-white text-slate-500'
                }`}
              >
                Use existing
              </button>
              <button
                type="button"
                onClick={() => setGithubSource('new')}
                className={`rounded-xl border px-3 py-2 text-xs font-medium transition ${
                  githubSource === 'new'
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                    : 'border-slate-200 bg-white text-slate-500'
                }`}
              >
                New connection
              </button>
            </div>

            {githubSource === 'existing' ? (
              connections.length === 0 ? (
                <p className="text-xs text-amber-700">
                  No connections yet. Pick "New connection" to create one.
                </p>
              ) : (
                <select
                  value={selectedConn}
                  onChange={(e) => setSelectedConn(e.target.value)}
                  className="app-input"
                >
                  {connections.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} ({c.host})
                    </option>
                  ))}
                </select>
              )
            ) : (
              <div className="space-y-3">
                <input
                  value={connName}
                  onChange={(e) => setConnName(e.target.value)}
                  placeholder="Connection name (optional)"
                  className="app-input"
                />
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => setNewKeyMode('generate')}
                    className={`rounded-xl border px-3 py-2 text-xs font-medium transition ${
                      newKeyMode === 'generate'
                        ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                        : 'border-slate-200 bg-white text-slate-500'
                    }`}
                  >
                    Generate key
                  </button>
                  <button
                    type="button"
                    onClick={() => setNewKeyMode('import')}
                    className={`rounded-xl border px-3 py-2 text-xs font-medium transition ${
                      newKeyMode === 'import'
                        ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                        : 'border-slate-200 bg-white text-slate-500'
                    }`}
                  >
                    Import key
                  </button>
                </div>
                {newKeyMode === 'import' && (
                  <>
                    <textarea
                      value={pubKeyInput}
                      onChange={(e) => setPubKeyInput(e.target.value)}
                      placeholder="ssh-ed25519 AAAA... (public key)"
                      rows={2}
                      className="app-textarea app-mono text-xs"
                    />
                    <textarea
                      value={privKeyInput}
                      onChange={(e) => setPrivKeyInput(e.target.value)}
                      placeholder="-----BEGIN OPENSSH PRIVATE KEY----- (private key)"
                      rows={3}
                      className="app-textarea app-mono text-xs"
                    />
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {generatedKey && (
          <div className="mb-4 rounded-[24px] border border-amber-200 bg-amber-50 p-4">
            <p className="mb-2 text-sm font-semibold text-amber-900">
              Save this private key now
            </p>
            <p className="mb-2 text-xs text-amber-800">
              Add the matching public key to your GitHub account. This private key is shown only once.
            </p>
            <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-2xl bg-white p-3 text-[11px] text-slate-700">
              {generatedKey}
            </pre>
          </div>
        )}

        {error && (
          <div className="mb-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="app-button-secondary"
          >
            Cancel
          </button>
          <button
            onClick={() => void handleSubmit()}
            disabled={loading}
            className="app-button-accent"
          >
            {loading ? 'Creating…' : 'Create workspace'}
          </button>
        </div>
      </div>
    </div>
  );
}
