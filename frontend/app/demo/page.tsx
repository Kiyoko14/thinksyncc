import type { Metadata } from "next";
import type { SVGProps } from "react";

export const metadata: Metadata = {
  title: "ThinkSync Demo",
  description: "Build and deploy real applications in minutes with AI",
};

function IconPrompt(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...props}>
      <path
        d="M8 9h8M8 13h6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M7 21l2.5-3H18a3 3 0 003-3V7a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3h1l0 3z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconCode(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...props}>
      <path
        d="M9 18l-6-6 6-6M15 6l6 6-6 6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconPackage(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...props}>
      <path
        d="M12 2l8 4-8 4-8-4 8-4z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
      />
      <path
        d="M4 6v12l8 4 8-4V6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
      />
      <path
        d="M12 10v12"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

function IconRocket(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...props}>
      <path
        d="M5 19c3.5 0 7-1.5 10-4.5S19 8 19 4c-4 0-7.5 1.5-10.5 4.5S5 15.5 5 19z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
      />
      <path
        d="M9 15l-3 3M14 10l3-3"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M10 12a2 2 0 104 0 2 2 0 00-4 0z"
        stroke="currentColor"
        strokeWidth="2"
      />
    </svg>
  );
}

function IconLink(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...props}>
      <path
        d="M10 13a5 5 0 010-7l1-1a5 5 0 017 7l-1 1"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M14 11a5 5 0 010 7l-1 1a5 5 0 01-7-7l1-1"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function DemoPage() {
  const steps = [
    {
      title: "User writes prompt",
      description: "Describe what you want to build in plain language.",
      Icon: IconPrompt,
    },
    {
      title: "AI generates code",
      description: "The system produces real project files and logic.",
      Icon: IconCode,
    },
    {
      title: "Dependencies installed",
      description: "Packages are installed automatically to match the project.",
      Icon: IconPackage,
    },
    {
      title: "App deployed",
      description: "The application is started on real servers.",
      Icon: IconRocket,
    },
    {
      title: "Live URL returned",
      description: "You get a shareable link to the running app.",
      Icon: IconLink,
    },
  ] as const;

  return (
    <main className="min-h-screen bg-gradient-to-b from-slate-50 via-white to-white text-slate-900 dark:from-slate-950 dark:via-slate-950 dark:to-slate-950 dark:text-slate-100">
      <div className="safe-top safe-bottom">
        <div className="mx-auto max-w-6xl px-4 py-12 sm:py-16">
          <section className="text-center">
            <div className="mx-auto inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white/70 px-3 py-1 text-xs font-medium text-slate-700 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 dark:text-slate-200">
              <span className="h-1.5 w-1.5 rounded-full bg-indigo-500" />
              ThinkSync • Public Demo
            </div>
            <h1 className="mt-6 text-balance text-4xl font-semibold tracking-tight sm:text-5xl">
              ThinkSync Demo
            </h1>
            <p className="mx-auto mt-4 max-w-2xl text-pretty text-base leading-7 text-slate-600 dark:text-slate-300 sm:text-lg">
              Build and deploy real applications in minutes with AI
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <a
                href="https://app.thinksync.art"
                target="_blank"
                rel="noreferrer"
                className="inline-flex w-full items-center justify-center rounded-xl bg-slate-900 px-5 py-3 text-sm font-semibold text-white shadow-sm ring-1 ring-slate-900/10 transition hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 dark:bg-white dark:text-slate-900 dark:hover:bg-slate-100 sm:w-auto"
              >
                Open App
              </a>
              <div className="w-full sm:w-auto">
                <div className="rounded-xl border border-slate-200 bg-white/70 px-4 py-3 text-left text-sm text-slate-600 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 dark:text-slate-300">
                  No login required • Mobile-friendly • Pitch-ready
                </div>
              </div>
            </div>
          </section>

          <div className="mt-12 grid gap-8">
            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <h2 className="text-lg font-semibold tracking-tight sm:text-xl">
                Problem
              </h2>
              <div className="mt-4 text-sm leading-7 text-slate-700 dark:text-slate-200 sm:text-base">
                Building and deploying applications is complex, time-consuming,
                and requires technical expertise. Many ideas never become real
                products because of infrastructure and coding barriers.
              </div>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <h2 className="text-lg font-semibold tracking-tight sm:text-xl">
                Solution
              </h2>
              <div className="mt-4 space-y-4 text-sm leading-7 text-slate-700 dark:text-slate-200 sm:text-base">
                <p>
                  ThinkSync is an AI execution platform that generates code,
                  installs dependencies, and runs applications on real servers
                  automatically — turning ideas into live products in minutes.
                </p>
              </div>

              <div className="mt-6 space-y-4">
                <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-white/10 dark:bg-slate-950">
                  <div className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Differentiation
                  </div>
                  <p className="mt-2 text-sm leading-7 text-slate-700 dark:text-slate-200 sm:text-base">
                    Unlike traditional AI tools, ThinkSync doesn't just generate
                    code — it executes it on real infrastructure and delivers a
                    live, working application.
                  </p>
                </div>

                <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-white/10 dark:bg-slate-950">
                  <div className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Impact
                  </div>
                  <p className="mt-2 text-sm leading-7 text-slate-700 dark:text-slate-200 sm:text-base">
                    This reduces development time from hours to minutes and
                    removes the need for manual setup, making software creation
                    accessible to everyone.
                  </p>
                </div>
              </div>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <h2 className="text-lg font-semibold tracking-tight sm:text-xl">
                    Product Demo
                  </h2>
                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                    Duration: 1 minutes
                  </p>
                </div>
                <a
                  href="https://youtube.com/shorts/hMpKfCASjlU?si=3GsgivSTIxcOgOeI"
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm font-medium text-indigo-600 hover:text-indigo-700 dark:text-indigo-400 dark:hover:text-indigo-300"
                >
                  Watch on YouTube
                </a>
              </div>
              <div className="mt-5 overflow-hidden rounded-xl ring-1 ring-slate-900/10 dark:ring-white/10">
                <div className="relative aspect-video bg-slate-100 dark:bg-slate-900">
                  <iframe
                    className="absolute inset-0 h-full w-full"
                    src="https://www.youtube.com/embed/hMpKfCASjlU?playsinline=1&rel=0"
                    title="ThinkSync Product Demo"
                    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                    allowFullScreen
                    referrerPolicy="strict-origin-when-cross-origin"
                  />
                </div>
              </div>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <h2 className="text-lg font-semibold tracking-tight sm:text-xl">
                Demo Description
              </h2>
              <div className="mt-4 space-y-4 text-sm leading-7 text-slate-700 dark:text-slate-200 sm:text-base">
                <p>
                  ThinkSync allows users to generate and run applications on real
                  servers using AI.
                </p>
                <p>
                  In this demo, a simple prompt is transformed into a fully
                  working application. The system automatically generates code,
                  installs dependencies, and deploys it.
                </p>
              </div>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <div className="flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
                <div>
                  <h2 className="text-lg font-semibold tracking-tight sm:text-xl">
                    Live Prototype
                  </h2>
                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                    This is a working MVP running on real servers
                  </p>
                </div>
                <div className="w-full sm:w-auto">
                  <p className="mb-2 text-sm text-slate-600 dark:text-slate-300">
                    Try a real working application generated by ThinkSync
                  </p>
                  <a
                    href="https://app.thinksync.art"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex w-full items-center justify-center rounded-xl bg-indigo-600 px-5 py-3 text-sm font-semibold text-white shadow-sm ring-1 ring-indigo-600/20 transition hover:bg-indigo-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 sm:w-auto"
                  >
                    Open Live Demo
                  </a>
                </div>
              </div>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <div className="flex items-end justify-between gap-4">
                <div>
                  <h2 className="text-lg font-semibold tracking-tight sm:text-xl">
                    How It Works
                  </h2>
                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                    From prompt to deployment in minutes.
                  </p>
                </div>
              </div>

              <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {steps.map((step, index) => (
                  <div
                    key={step.title}
                    className="group rounded-2xl border border-slate-200 bg-white p-5 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md dark:border-white/10 dark:bg-slate-950"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-indigo-50 text-indigo-700 ring-1 ring-indigo-100 dark:bg-indigo-500/10 dark:text-indigo-300 dark:ring-indigo-500/20">
                        <step.Icon className="h-6 w-6" />
                      </div>
                      <div className="flex h-7 items-center rounded-full bg-slate-100 px-2 text-xs font-semibold text-slate-700 dark:bg-white/5 dark:text-slate-200">
                        Step {index + 1}
                      </div>
                    </div>
                    <h3 className="mt-4 text-base font-semibold tracking-tight">
                      {step.title}
                    </h3>
                    <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">
                      {step.description}
                    </p>
                  </div>
                ))}
              </div>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <h2 className="text-lg font-semibold tracking-tight sm:text-xl">
                Why It Matters
              </h2>
              <ul className="mt-4 space-y-3 text-sm leading-7 text-slate-700 dark:text-slate-200 sm:text-base">
                {[
                  "No coding required",
                  "No server setup",
                  "Instant deployment",
                  "Real working applications",
                ].map((item) => (
                  <li key={item} className="flex gap-3">
                    <span className="mt-3 h-1.5 w-1.5 flex-none rounded-full bg-indigo-500 dark:bg-indigo-400" />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
              <p className="mt-5 text-sm leading-7 text-slate-600 dark:text-slate-300 sm:text-base">
                ThinkSync removes the biggest barriers between ideas and
                execution.
              </p>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <h2 className="text-lg font-semibold tracking-tight sm:text-xl">
                    Example Prompt
                  </h2>
                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                    Try this to see a fast end-to-end build.
                  </p>
                </div>
                <a
                  href="https://app.thinksync.art"
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm font-medium text-indigo-600 hover:text-indigo-700 dark:text-indigo-400 dark:hover:text-indigo-300"
                >
                  Open ThinkSync
                </a>
              </div>

              <div className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-white/10 dark:bg-slate-950">
                <div className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  Prompt
                </div>
                <div className="mt-3 rounded-xl bg-slate-50 p-4 font-mono text-sm text-slate-900 ring-1 ring-slate-900/10 dark:bg-white/5 dark:text-slate-100 dark:ring-white/10">
                  Create a Telegram bot
                </div>
              </div>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white/70 p-5 text-center shadow-sm backdrop-blur dark:border-white/10 dark:bg-white/5 sm:p-8">
              <h2 className="text-balance text-xl font-semibold tracking-tight sm:text-2xl">
                Start building with ThinkSync today
              </h2>
              <div className="mt-6 flex justify-center">
                <a
                  href="https://app.thinksync.art"
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex w-full max-w-xs items-center justify-center rounded-xl bg-slate-900 px-5 py-3 text-sm font-semibold text-white shadow-sm ring-1 ring-slate-900/10 transition hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 dark:bg-white dark:text-slate-900 dark:hover:bg-slate-100"
                >
                  Open App →
                </a>
              </div>
            </section>
          </div>

          <footer className="mt-12 text-center text-xs text-slate-500 dark:text-slate-400">
            © {new Date().getFullYear()} ThinkSync. Demo page.
          </footer>
        </div>
      </div>
    </main>
  );
}
