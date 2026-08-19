"use client";

import GoogleSignIn from "@/components/GoogleSignIn";

export default function LoginForm() {
  return (
    <div className="w-full max-w-md">
      <div className="app-surface overflow-hidden">
        <div className="subtle-grid border-b border-slate-200/70 px-8 py-8">
          <div className="inline-flex rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-emerald-700">
            ThinkSync
          </div>
          <h1 className="mt-5 text-3xl font-semibold tracking-tight text-slate-950">
            AI server operations, workspace execution, and deployment.
          </h1>
          <p className="mt-3 text-sm leading-6 text-slate-600">
            Sign in to connect servers, create workspaces, and let the agent do the work.
          </p>
        </div>

        <div className="px-8 py-8">
          <GoogleSignIn />

          <p className="mt-4 text-center text-xs text-slate-400">
            By continuing you agree to ThinkSync&apos;s Terms and Privacy Policy.
          </p>
        </div>
      </div>
    </div>
  );
}
