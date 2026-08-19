"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { googleLogin } from "@/services/auth";

const GSI_SCRIPT_SRC = "https://accounts.google.com/gsi/client";

/**
 * Loads the Google Identity Services client script once and resolves with the
 * global `google.accounts.id` namespace. Avoids adding a dependency and keeps
 * the token flow entirely client-side.
 */
function loadGoogleScript(): Promise<GoogleIdService> {
  return new Promise((resolve, reject) => {
    const g = typeof window !== "undefined" ? window.google : undefined;
    if (g?.accounts?.id) {
      resolve(g.accounts.id);
      return;
    }
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${GSI_SCRIPT_SRC}"]`,
    );
    const onReady = () => {
      const wg = window.google;
      if (wg?.accounts?.id) resolve(wg.accounts.id);
    };
    if (existing) {
      if (window.google?.accounts?.id) {
        resolve(window.google.accounts.id);
      } else {
        existing.addEventListener("load", onReady);
      }
      return;
    }
    const script = document.createElement("script");
    script.src = GSI_SCRIPT_SRC;
    script.async = true;
    script.defer = true;
    script.onload = onReady;
    script.onerror = () =>
      reject(new Error("Failed to load Google Identity Services."));
    document.body.appendChild(script);
  });
}

export default function GoogleSignIn() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const initialized = useRef(false);

  const clientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "";

  const handleCredential = useCallback(
    async (idToken: string) => {
      if (loading) return;
      setLoading(true);
      setError(null);
      try {
        await googleLogin(idToken);
        router.push("/servers");
      } catch (err: unknown) {
        setError(
          err instanceof Error
            ? err.message
            : "Google sign-in failed. Please try again.",
        );
      } finally {
        setLoading(false);
      }
    },
    [loading, router],
  );

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    if (!clientId) {
      setError("Google sign-in is not configured (missing client ID).");
      return;
    }
    let cancelled = false;
    loadGoogleScript()
      .then((id) => {
        if (cancelled) return;
        // one-tap / button prompt; we render our own button and call
        // google.accounts.id.prompt manually so the flow is explicit.
        id.initialize({
          client_id: clientId,
          callback: (response: { credential: string }) =>
            void handleCredential(response.credential),
          auto_select: false,
          cancel_on_tap_outside: true,
        });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Could not initialize Google sign-in.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [clientId, handleCredential]);

  const onClick = () => {
    if (loading) return;
    const gid = window.google?.accounts?.id;
    if (!gid) {
      setError("Google sign-in is still loading. Please try again.");
      return;
    }
    // Trigger the Google account chooser. The callback receives the ID token.
    gid.prompt?.((notification: { isNotDisplayed?: boolean; isSkipped?: boolean }) => {
      if (notification.isNotDisplayed || notification.isSkipped) {
        setError("Google sign-in was dismissed. Please try again.");
      }
    });
  };

  return (
    <div className="w-full">
      {error ? (
        <div className="mb-4 rounded-3xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      ) : null}

      <button
        type="button"
        onClick={onClick}
        disabled={loading || !clientId}
        className="inline-flex w-full items-center justify-center gap-3 rounded-3xl border border-slate-200 bg-slate-950 px-4 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {loading ? (
          <span className="inline-flex items-center gap-2">
            <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8v8H4z"
              />
            </svg>
            Signing in…
          </span>
        ) : (
          <>
            <GoogleGlyph />
            Continue with Google
          </>
        )}
      </button>
    </div>
  );
}

function GoogleGlyph() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}
