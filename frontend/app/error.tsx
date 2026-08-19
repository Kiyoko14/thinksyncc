"use client";

import { useEffect } from "react";
import Link from "next/link";

/**
 * Global route-level error boundary (Next.js App Router convention).
 * Catches render/runtime errors in any route segment and shows a recoverable
 * fallback UI instead of a blank white screen. Boundaries must be Client
 * Components and accept the standard `error` / `reset` props.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Structured logging only — never log secrets. Surface a digest for support.
    console.error("[GlobalError]", error?.message, error?.digest);
  }, [error]);

  return (
    <main className="flex min-h-screen items-center justify-center bg-transparent px-4 text-slate-900">
      <div className="app-surface w-full max-w-md p-8 text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Something went wrong</h1>
        <p className="mt-2 text-sm text-slate-500">
          An unexpected error occurred. You can retry or return to a safe page.
        </p>
        {error?.digest ? (
          <p className="mt-3 text-xs text-slate-400 font-mono">Ref: {error.digest}</p>
        ) : null}
        <div className="mt-6 flex items-center justify-center gap-3">
          <button
            type="button"
            onClick={reset}
            className="app-button-accent"
          >
            Try again
          </button>
          <Link
            href="/servers"
            className="app-button-secondary"
          >
            Go to Servers
          </Link>
        </div>
      </div>
    </main>
  );
}
