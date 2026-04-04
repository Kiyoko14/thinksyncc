"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import BottomNav from "@/components/BottomNav";
import { getToken, logout } from "@/services/auth";

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
            onClick={() => { logout(); router.replace("/login"); }}
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

      <main className="px-4 pt-6 space-y-4">
        <div>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="text-gray-500 text-sm mt-0.5">AI-powered DevOps Platform</p>
        </div>

        {/* Quick actions */}
        <div className="grid grid-cols-1 gap-3">
          <a
            href="/servers"
            className="bg-gray-900 border border-gray-800 rounded-2xl p-5 active:scale-[0.98] transition-transform"
          >
            <div className="flex items-center gap-4">
              <div className="w-12 h-12 rounded-xl bg-blue-600/10 border border-blue-600/20 flex items-center justify-center flex-shrink-0">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="2" y="2" width="20" height="8" rx="2" />
                  <rect x="2" y="14" width="20" height="8" rx="2" />
                  <circle cx="6" cy="6" r="1" fill="#60a5fa" stroke="none" />
                  <circle cx="6" cy="18" r="1" fill="#60a5fa" stroke="none" />
                </svg>
              </div>
              <div>
                <p className="font-semibold text-white">Servers</p>
                <p className="text-gray-500 text-sm mt-0.5">Manage SSH-connected servers</p>
              </div>
              <svg className="ml-auto text-gray-600" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </div>
          </a>
        </div>

        {/* Info cards */}
        <div className="bg-gray-900 border border-gray-800 rounded-2xl p-5">
          <p className="text-xs font-medium text-gray-400 uppercase tracking-wide mb-3">How it works</p>
          <div className="space-y-3">
            {[
              { n: "1", text: "Add a server with SSH credentials" },
              { n: "2", text: "A workspace is automatically created" },
              { n: "3", text: "Tap the server to chat with AI about it" },
            ].map(({ n, text }) => (
              <div key={n} className="flex items-start gap-3">
                <span className="w-5 h-5 rounded-full bg-blue-600/20 text-blue-400 text-[11px] font-bold flex items-center justify-center flex-shrink-0 mt-0.5">
                  {n}
                </span>
                <p className="text-gray-300 text-sm">{text}</p>
              </div>
            ))}
          </div>
        </div>
      </main>

      <BottomNav />
    </div>
  );
}
