"use client";

import LoginForm from "@/components/LoginForm";

export default function LoginPage() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 safe-top safe-bottom">
      <div className="mx-auto flex min-h-screen max-w-2xl items-center justify-center px-4 py-10 sm:px-6">
        <LoginForm />
      </div>
    </div>
  );
}
