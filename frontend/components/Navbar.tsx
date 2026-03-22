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
    <nav className="bg-gray-900 border-b border-gray-800">
      <div className="max-w-5xl mx-auto px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-6">
          <Link href="/dashboard" className="text-white font-bold text-lg tracking-tight">
            ThinkSync
          </Link>
          <Link
            href="/dashboard"
            className="text-gray-400 hover:text-white text-sm transition"
          >
            Dashboard
          </Link>
          <Link
            href="/servers"
            className="text-gray-400 hover:text-white text-sm transition"
          >
            Servers
          </Link>
        </div>

        <button
          onClick={handleLogout}
          className="text-gray-400 hover:text-white text-sm transition"
        >
          Logout
        </button>
      </div>
    </nav>
  );
}
