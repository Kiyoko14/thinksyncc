"use client";

import LoginForm from "@/components/LoginForm";

export default function LoginPage() {
  return (
    <div className="min-h-screen bg-gray-950 text-white safe-top safe-bottom">
      <div className="mx-auto flex min-h-screen max-w-6xl items-center justify-center px-6 py-12">
        <LoginForm />
      </div>
    </div>
  );
}
