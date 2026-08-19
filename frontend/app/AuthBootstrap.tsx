"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { validateStoredToken } from "@/services/auth";

function isPublicPath(pathname: string | null): boolean {
  if (!pathname) return false;
  // Normalize trailing slash so public routes match with or without a slash
  // regardless of Next.js trailingSlash config (parity: "/login" === "/login/").
  const normalized = pathname.replace(/\/+$/, "") || "/";
  return (
    normalized === "/" ||
    normalized === "/login" ||
    normalized === "/demo"
  );
}

export default function AuthBootstrap({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    const token = validateStoredToken();
    const publicRoute = isPublicPath(pathname);

    if (!token && !publicRoute) {
      router.replace("/login");
      return;
    }

    setChecking(false);
  }, [pathname, router]);

  if (checking && !isPublicPath(pathname)) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[radial-gradient(circle_at_top,rgba(15,118,110,0.08),transparent_32%),linear-gradient(180deg,#f8fafc,#eef2f7)] text-slate-900">
        <div className="app-panel flex items-center gap-3 px-5 py-4">
          <svg className="h-5 w-5 animate-spin text-emerald-600" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-100" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
          </svg>
          <div>
            <p className="text-sm font-semibold text-slate-900">Loading ThinkSync</p>
            <p className="text-xs text-slate-500">Verifying your session</p>
          </div>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
