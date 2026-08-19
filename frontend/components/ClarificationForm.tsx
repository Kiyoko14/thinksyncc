"use client";

import { useState } from "react";
import { HelpCircle, Send, Loader2, Lock } from "lucide-react";
import type {
  ClarificationForm as ClarificationFormType,
  ClarificationFormQuestion,
  ClarificationFormAnswer,
  ClarificationFormSubmission,
} from "@/services/api";

/**
 * Generic clarification form renderer.
 *
 * This component is a PURE RENDERER.  It:
 *   • renders title / description
 *   • renders each question's input, choices, placeholder, examples, validation
 *   • collects answers in client state
 *   • performs client-side validation (mirroring the backend schema)
 *   • submits ONE structured ClarificationFormSubmission on Submit
 *
 * It NEVER generates questions, NEVER infers fields, and contains NO project
 * logic.  The schema comes entirely from the backend (ClarificationForm).
 */
export default function ClarificationForm({
  form,
  jobId,
  conversationId,
  onSubmit,
  onCancel,
  submitting,
  error,
}: {
  form: ClarificationFormType;
  jobId: string;
  conversationId?: string | null;
  onSubmit: (submission: ClarificationFormSubmission) => void;
  onCancel?: () => void;
  submitting: boolean;
  error: string | null;
}) {
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [selectedChoice, setSelectedChoice] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  const setField = (q: ClarificationFormQuestion, value: unknown, choice?: string) => {
    setValues((v) => ({ ...v, [q.id]: value }));
    if (choice !== undefined) {
      setSelectedChoice((c) => ({ ...c, [q.id]: choice }));
    }
    // Clear any prior error for this field on change.
    setErrors((e) => {
      if (!e[q.id]) return e;
      const next = { ...e };
      delete next[q.id];
      return next;
    });
  };

  // Client-side validation (authoritative validation also runs on the backend).
  const validate = (): boolean => {
    const next: Record<string, string> = {};
    for (const q of form.questions) {
      const choice = selectedChoice[q.id];
      const raw = values[q.id];
      const hasChoice = choice !== undefined && choice !== "";
      const hasValue = raw !== undefined && raw !== null && String(raw).trim() !== "";

      if (q.required && !hasChoice && !hasValue) {
        next[q.id] = "This question is required.";
        continue;
      }
      if (q.choices.length > 0 && (hasChoice || hasValue)) {
        const allowed = q.choices.map((c) => c.value);
        const provided = hasChoice ? choice! : String(raw);
        if (!allowed.includes(provided)) {
          next[q.id] = "Please pick one of the available options.";
          continue;
        }
      }
      if (q.type === "number" || q.type === "port") {
        if (hasValue && isNaN(Number(raw))) {
          next[q.id] = "Must be a number.";
          continue;
        }
        if (q.type === "port") {
          const n = Number(raw);
          if (hasValue && (n < 1 || n > 65535)) {
            next[q.id] = "Port must be between 1 and 65535.";
            continue;
          }
        }
      }
      if (q.type === "boolean" && hasValue) {
        const s = String(raw).toLowerCase();
        if (!["true", "false", "0", "1", "yes", "no"].includes(s)) {
          next[q.id] = "Must be yes/no.";
          continue;
        }
      }
      if (q.validation?.pattern_description && hasValue && q.validation?.regex) {
        try {
          if (!new RegExp(q.validation.regex).test(String(raw))) {
            next[q.id] = q.validation.pattern_description;
            continue;
          }
        } catch {
          // Invalid regex in schema — backend is authoritative; skip client check.
        }
      }
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const buildAnswers = (): ClarificationFormAnswer[] => {
    return form.questions.map((q) => {
      const choice = selectedChoice[q.id];
      const raw = values[q.id];
      const useChoice = choice !== undefined && choice !== "";
      // For choice-only questions the value is the selected label.
      const value = useChoice ? choice : raw ?? null;
      return {
        question_id: q.id,
        required_field: q.required_field,
        value: q.type === "boolean" && !useChoice ? raw : value,
        selected_choice: useChoice ? choice : null,
      } as ClarificationFormAnswer;
    });
  };

  const handleSubmit = () => {
    if (submitting) return;
    if (!validate()) return;
    const submission: ClarificationFormSubmission = {
      clarification_id: form.id,
      answers: buildAnswers(),
    };
    onSubmit(submission);
  };

  return (
    <div
      className="app-panel border-amber-200 bg-amber-50/90 p-4 shadow-sm shadow-amber-100/60"
      role="alertdialog"
      aria-label={form.title}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5">
          {form.questions.some((q) => q.secret) ? (
            <Lock size={18} className="text-amber-600" />
          ) : (
            <HelpCircle size={18} className="text-amber-600" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-amber-950">{form.title}</p>
          <p className="mt-0.5 text-sm text-amber-800">{form.description}</p>
        </div>
      </div>

      <div className="mt-3 rounded-2xl border border-amber-200 bg-white/80 px-3 py-2 text-sm text-slate-700">
        ThinkSync paused here to resolve one required input.
      </div>

      <div className="mt-3 space-y-4">
        {form.questions.map((q) => (
          <FormField
            key={q.id}
            q={q}
            value={values[q.id]}
            choice={selectedChoice[q.id]}
            error={errors[q.id]}
            onChange={setField}
          />
        ))}
      </div>

      {error ? <p className="mt-3 rounded-2xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}

      <div className="mt-4 flex items-center gap-2">
        <button
          type="button"
          disabled={submitting}
          onClick={handleSubmit}
          className="inline-flex items-center gap-2 rounded-2xl bg-amber-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          Submit
        </button>
        {onCancel ? (
          <button
            type="button"
            disabled={submitting}
            onClick={onCancel}
            className="rounded-2xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-600 transition hover:bg-slate-50"
          >
            Dismiss
          </button>
        ) : null}
      </div>
    </div>
  );
}

function FormField({
  q,
  value,
  choice,
  error,
  onChange,
}: {
  q: ClarificationFormQuestion;
  value: unknown;
  choice: string | undefined;
  error?: string;
  onChange: (q: ClarificationFormQuestion, value: unknown, choice?: string) => void;
}) {
  const inputId = `clarif-${q.id}`;
  const hasError = Boolean(error);

  return (
    <div className="rounded-2xl border border-slate-200 bg-white/80 px-3 py-3">
      <label htmlFor={inputId} className="block text-sm font-medium text-slate-800">
        {q.title}
        {q.required ? <span className="ml-1 text-red-500">*</span> : null}
        {q.secret ? <span className="ml-2 text-xs font-normal text-amber-700">(secret)</span> : null}
      </label>
      {q.description ? (
        <p className="mt-0.5 text-xs text-slate-500">{q.description}</p>
      ) : null}

      {/* Predefined choices render first (the backend supplies actions). */}
      {q.choices.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {q.choices.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => onChange(q, undefined, c.value)}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
                choice === c.value
                  ? "border-amber-500 bg-amber-100 text-amber-900"
                : "border-slate-300 bg-white text-slate-600 hover:border-amber-400"
              }`}
            >
              {c.label}
            </button>
          ))}
        </div>
      ) : (
        <div className="mt-2">
          <InputControl q={q} value={value} onChange={onChange} hasError={hasError} inputId={inputId} />
        </div>
      )}

      {q.example ? (
        <p className="mt-1 text-xs text-slate-400">Example: {q.example}</p>
      ) : null}
      {hasError ? <p className="mt-1 text-xs text-red-600">{error}</p> : null}
    </div>
  );
}

function InputControl({
  q,
  value,
  onChange,
  hasError,
  inputId,
}: {
  q: ClarificationFormQuestion;
  value: unknown;
  onChange: (q: ClarificationFormQuestion, value: unknown, choice?: string) => void;
  hasError: boolean;
  inputId: string;
}) {
  const border = hasError
    ? "border-red-300 focus:border-red-400 focus:ring-red-200"
    : "border-amber-200 focus:border-amber-400 focus:ring-amber-200";

  if (q.type === "textarea") {
    return (
      <textarea
        id={inputId}
        value={(value as string) ?? ""}
        onChange={(e) => onChange(q, e.target.value)}
        rows={3}
        placeholder={q.placeholder}
        className={`mt-1 w-full resize-none rounded-2xl border bg-white px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 ${border}`}
      />
    );
  }

  if (q.type === "boolean") {
    return (
      <div className="mt-2 flex flex-wrap gap-2">
        {[
          { label: "Yes", v: "true" },
          { label: "No", v: "false" },
        ].map((opt) => (
          <button
            key={opt.v}
            type="button"
            onClick={() => onChange(q, opt.v)}
            className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
              value === opt.v
                ? "border-amber-500 bg-amber-100 text-amber-900"
                : "border-slate-300 bg-white text-slate-600 hover:border-amber-400"
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
    );
  }

  const inputType =
    q.type === "secret" || q.type === "password" || q.type === "api_key" || q.type === "ssh_key"
      ? "password"
      : q.type === "number" || q.type === "port"
      ? "number"
      : q.type === "email"
      ? "email"
      : q.type === "url"
      ? "url"
      : "text";

  return (
    <input
      id={inputId}
      type={inputType}
      value={(value as string) ?? ""}
      onChange={(e) => onChange(q, e.target.value)}
      placeholder={q.placeholder}
      min={q.validation?.min ?? undefined}
      max={q.validation?.max ?? undefined}
      className={`mt-1 w-full rounded-2xl border bg-white px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 ${border}`}
    />
  );
}
