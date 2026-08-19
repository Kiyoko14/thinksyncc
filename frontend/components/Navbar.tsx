"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { logout } from "@/services/auth";

export default function Navbar() {
  const router = useRouter();

  const handleLogout = () => {
    logout();
    router.replace("/login");
  };

  return (
    <nav className="border-b border-slate-200/80 bg-white/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6 lg:px-8">
        <div className="flex items-center gap-6">
          <Link href="/dashboard" className="text-lg font-semibold tracking-tight text-slate-950">
            ThinkSync
          </Link>
          <Link
            href="/dashboard"
            className="text-sm text-slate-500 transition hover:text-slate-950"
          >
            Dashboard
          </Link>
          <Link
            href="/servers"
            className="text-sm text-slate-500 transition hover:text-slate-950"
          >
            Servers
          </Link>
        </div>

        <button
          onClick={handleLogout}
          className="app-button-secondary"
        >
          Logout
        </button>
      </div>
    </nav>
  );
}
