"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Navbar from "@/components/Navbar";
import ServerCard from "@/components/ServerCard";
import { getServers, addServer, deleteServer } from "@/services/api";
import { getToken } from "@/services/auth";
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
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<ServerCreatePayload>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    loadServers();
  }, [router]);

  const loadServers = async () => {
    try {
      const data = await getServers();
      setServers(data);
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
      setShowForm(false);
      setForm(EMPTY_FORM);
      await loadServers();
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : "Failed to add server");
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (id: string) => {
    await deleteServer(id);
    setServers((prev) => prev.filter((s) => s.id !== id));
  };

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <Navbar />
      <main className="max-w-5xl mx-auto px-4 py-10">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-3xl font-bold">Servers</h1>
          <button
            onClick={() => {
              setShowForm(!showForm);
              setFormError("");
            }}
            className="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg text-sm font-semibold transition"
          >
            {showForm ? "Cancel" : "+ Add Server"}
          </button>
        </div>

        {showForm && (
          <form
            onSubmit={handleAdd}
            className="bg-gray-900 border border-gray-800 rounded-xl p-6 mb-6 space-y-4"
          >
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-gray-400 mb-1">Name</label>
                <input
                  className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 border border-gray-700 focus:outline-none focus:border-blue-500"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  required
                />
              </div>
              <div>
                <label className="block text-sm text-gray-400 mb-1">Host / IP</label>
                <input
                  className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 border border-gray-700 focus:outline-none focus:border-blue-500"
                  value={form.host}
                  onChange={(e) => setForm({ ...form, host: e.target.value })}
                  required
                />
              </div>
              <div>
                <label className="block text-sm text-gray-400 mb-1">SSH User</label>
                <input
                  className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 border border-gray-700 focus:outline-none focus:border-blue-500"
                  value={form.ssh_user}
                  onChange={(e) => setForm({ ...form, ssh_user: e.target.value })}
                  required
                />
              </div>
              <div>
                <label className="block text-sm text-gray-400 mb-1">SSH Port</label>
                <input
                  type="number"
                  min={1}
                  max={65535}
                  className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 border border-gray-700 focus:outline-none focus:border-blue-500"
                  value={form.ssh_port}
                  onChange={(e) => setForm({ ...form, ssh_port: Number(e.target.value) })}
                  required
                />
              </div>
            </div>

            <div>
              <label className="block text-sm text-gray-400 mb-1">Auth Method</label>
              <select
                className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 border border-gray-700 focus:outline-none focus:border-blue-500"
                value={form.ssh_auth_method}
                onChange={(e) =>
                  setForm({ ...form, ssh_auth_method: e.target.value as "password" | "key" })
                }
              >
                <option value="password">Password</option>
                <option value="key">SSH Key</option>
              </select>
            </div>

            {form.ssh_auth_method === "password" && (
              <div>
                <label className="block text-sm text-gray-400 mb-1">SSH Password</label>
                <input
                  type="password"
                  autoComplete="new-password"
                  className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 border border-gray-700 focus:outline-none focus:border-blue-500"
                  value={form.ssh_password ?? ""}
                  onChange={(e) => setForm({ ...form, ssh_password: e.target.value })}
                />
              </div>
            )}

            {form.ssh_auth_method === "key" && (
              <div>
                <label className="block text-sm text-gray-400 mb-1">SSH Private Key</label>
                <textarea
                  rows={6}
                  className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 border border-gray-700 font-mono text-xs focus:outline-none focus:border-blue-500"
                  value={form.ssh_key ?? ""}
                  onChange={(e) => setForm({ ...form, ssh_key: e.target.value })}
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                />
              </div>
            )}

            {formError && <p className="text-red-400 text-sm">{formError}</p>}

            <button
              type="submit"
              disabled={submitting}
              className="bg-green-600 hover:bg-green-700 px-6 py-2 rounded-lg text-sm font-semibold transition disabled:opacity-50"
            >
              {submitting ? "Saving…" : "Save Server"}
            </button>
          </form>
        )}

        {loading ? (
          <p className="text-gray-500">Loading servers…</p>
        ) : servers.length === 0 ? (
          <p className="text-gray-500">No servers added yet.</p>
        ) : (
          <div className="grid gap-4">
            {servers.map((server) => (
              <ServerCard key={server.id} server={server} onDelete={handleDelete} />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
