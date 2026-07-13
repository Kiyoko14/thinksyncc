"use client";

import GoogleSignIn from "@/components/GoogleSignIn";

export default function LoginForm() {
  return (
    <div className="w-full max-w-md">
      <div className="rounded-[32px] border border-slate-200 bg-white p-8 shadow-[0_20px_80px_-40px_rgba(15,23,42,0.15)]">
        <div className="mb-6">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
            ThinkSync
          </h1>
          <p className="mt-2 text-sm text-slate-500">
            Sign in to your AI DevOps workspace.
          </p>
        </div>

        <GoogleSignIn />

        <p className="mt-4 text-center text-xs text-slate-400">
          By continuing you agree to ThinkSync&apos;s Terms and Privacy Policy.
        </p>
      </div>
    </div>
  );
}
