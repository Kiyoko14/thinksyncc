"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Navbar from "@/components/Navbar";
import { getToken } from "@/services/auth";

export default function DashboardPage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
    } else {
      setReady(true);
    }
  }, [router]);

  if (!ready) return null;

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <Navbar />
      <main className="max-w-5xl mx-auto px-4 py-10">
        <h1 className="text-3xl font-bold mb-2">Dashboard</h1>
        <p className="text-gray-400 mb-8">Welcome to ThinkSync v2.</p>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <a
            href="/servers"
            className="bg-gray-900 border border-gray-800 rounded-xl p-6 hover:border-blue-600 transition"
          >
            <h2 className="text-lg font-semibold mb-1">Servers</h2>
            <p className="text-gray-400 text-sm">Manage SSH-connected servers.</p>
          </a>
        </div>
      </main>
    </div>
  );
}
