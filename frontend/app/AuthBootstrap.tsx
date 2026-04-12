"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { validateStoredToken } from "@/services/auth";

function isPublicPath(pathname: string | null): boolean {
  if (!pathname) return false;
  return pathname === "/" || pathname === "/login" || pathname === "/signup";
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
      <div className="min-h-screen bg-gray-950 text-white flex items-center justify-center">
        <svg className="animate-spin h-6 w-6 text-blue-500" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
        </svg>
      </div>
    );
  }

  return <>{children}</>;
}
