# ThinkSync — Sprint 3C.D: Adaptive Clarification Engine

**Date:** 2026-07-13
**Status:** COMPLETE (with documented limitations)
**Scope:** Extension of the existing clarification / requirement-discovery / event-wait architecture
**Rule adherence:** No redesign, no replacement of working modules, backward compatible, reuse-first.

---

## 1. Architecture

The Adaptive Clarification Engine is a **thin decision layer** placed *before planning* in the
existing `AgentService.run_agent_pipeline` orchestration. It does **not** replace the rule-based
`ClarificationEngine`; instead it *composes* it and the surrounding services into an adaptive
decision flow:

```
user request (JobCreate)
        │
        ▼
requirement discovery  ──►  project_spec
        │
        ▼
AdaptiveClarificationEngine.evaluate(...)        ◄── NEW (services/adaptive_clarification.py)
        ├─ requirement completeness score (internal)
        ├─ implementation-intelligence check (skip if report already answers)
        ├─ conversation awareness (history + prior answers + prior questions)
        ├─ repository awareness (spec-derived architecture/features)
        ├─ candidate questions (reuses ClarificationEngine.evaluate_review + spec gaps)
        ├─ de-duplication + quality gate
        └─ cost-aware decision:  CONTINUE | ASK | SAFE_ASSUME
        │
        ├── CONTINUE ─────────────────────► proceed to planning/execution
        ├── SAFE_ASSUME (low risk) ───────► proceed, assumptions recorded
        └── ASK ──► raise ClarificationSuspendSignal
                        │
                        ▼
              EventWaitEngine.register + suspend (worker released)
              POST /jobs/{id}/clarification-reply ──► CLARIFICATION_REPLY signal
                        │
                        ▼
              EventWaitEngine.await_clarification_reply ──► record answer ──► re-dispatch run_job
                        (multi-turn: re-enters evaluate with new context; no restart)
```

The engine is **pure logic** (no DB/LLM I/O inside it). All reads are performed by the
orchestrator via the *existing* services and injected as arguments. This keeps the engine
unit-testable and free of global state.

---

## 2. Adaptive Clarification Flow

Per the brief's required decision flow, implemented as discrete stages inside
`AdaptiveClarificationEngine.evaluate`:

1. **Requirement Completeness** → internal score in `[0,1]` (never user-facing). Heuristic over
   objective clarity, spec readiness, `missing_info`, and review issues/blocking flags.
2. **Repository Knowledge** → `repository_snippet` (spec-derived architecture/features/framework)
   is matched against candidate questions; if the repo already answers a question, it is downgraded
   to a safe assumption instead of being asked.
3. **Conversation Context** → `prior_questions` + `existing_answers` + chat `conversation_history`
   are normalized and used to suppress already-asked/answered questions.
4. **Existing Specification** → `project_spec.readiness` / `missing_info` feed completeness and the
   implementation-intelligence skip.
5. **Implementation Risk** → questions carry `blocking` / `priority` (CRITICAL..LOW); high-risk or
   blocking gaps force an ASK; low-risk non-blocking gaps may SAFE_ASSUME.
6. **Decision** → `CONTINUE` | `ASK` | `SAFE_ASSUME`, cost-aware.

---

## 3. Files Modified

| File | Change |
|------|--------|
| `services/adaptive_clarification.py` | **NEW** — `AdaptiveClarificationEngine`, `ClarificationDecision`, `ClarificationAction`, `AssumptionLevel` |
| `services/agent_service.py` | Inject adaptive decision before planning; catch `ClarificationSuspendSignal`; suspend via EventWaitEngine; spawn clarification-waiter |
| `services/event_wait_engine.py` | Add `CLARIFICATION_REPLY` event constant; add `await_clarification_reply` driver (reuses register/wait/signal) |
| `services/interactive_wait.py` | Add `record_clarification_answer` + `get_clarification_session` (persist answers into `jobs.clarification_session`) |
| `models/agent.py` | Add `ClarificationSuspendSignal` exception (model-level, avoids circular import) |
| `routers/agents.py` | Add `POST /jobs/{job_id}/clarification-reply` endpoint (signals `CLARIFICATION_REPLY`) |
| `sprint_3cd_self_test.py` | **NEW** — 8 unit tests for the adaptive decision logic |

*(`models/job.py` already had `WAITING_FOR_USER` etc. from 3C.C; reused here, no new change.)*

---

## 4. Components Reused

- **`ClarificationEngine`** (`services/clarification_engine.py`) — `evaluate_review` reused to
  translate a `SpecificationReview` into candidate questions (deterministic core preserved).
- **`ImplementationIntelligence`** semantics — `_implementation_report_sufficient` reuses the
  existing strategy/files/validation shape to *skip* clarification when the report already answers.
- **`ProjectSpecification`** — `readiness`, `missing_info`, `assumptions`, `features`,
  `framework`, `project_type` consumed for completeness + repository awareness.
- **`ChatService.get_recent_context_messages`** — conversation-history awareness.
- **`InteractiveWaitEngine`** — `get_clarification_session` / `record_clarification_answer` reuse
  the existing `jobs` persistence path (no new store).
- **`EventWaitEngine`** — `register` / `wait` / `signal` / `clear` reused for suspend + resume;
  only a new event constant + a clarification-specific driver added.
- **`ClarificationSession` / `ClarificationQuestion` / `StructuredUserReply` / `ReplyType`** —
  existing models reused to persist questions and answers.
- **`models/job.py` `JobStatus.WAITING_FOR_USER`** — reused for the suspended job state.

---

## 5. Components Extended

- **`EventWaitEngine`** — extended with `CLARIFICATION_REPLY` event and `await_clarification_reply`
  driver (parks on the bus, records the answer, re-dispatches). The approval `await_and_resume`
  driver is untouched.
- **`InteractiveWaitEngine`** — extended with clarification session persistence helpers, reusing the
  existing `jobs` table and `ClarificationSession` model.
- **`agent_service.run_agent_pipeline`** — extended (not rewritten) with the adaptive decision hook;
  the existing planning/execution tail is unchanged.

---

## 6. New Components

- **`AdaptiveClarificationEngine`** (`services/adaptive_clarification.py`) — the only net-new
  component, justified because the brief requires a full adaptive decision flow that the existing
  deterministic `ClarificationEngine` does not provide. It *composes* rather than duplicates.
- **`ClarificationSuspendSignal`** — a model-level signal mirroring `ApprovalSuspendSignal`, needed
  so `agent_service` can catch it without a circular import (same pattern as 3C.C).
- **`/clarification-reply` router endpoint** — a new event source, mirroring the existing `/reply`
  endpoint (no new handler logic, just a typed signal).

---

## 7. Requirement Completeness Analysis

`AdaptiveClarificationEngine._score_completeness` produces an **internal** score (0.0–1.0), never
exposed to the user:
- base 0.4 for an existing objective;
- +0.1 for objective length ≥4 words;
- +0.1 for a concrete action verb (build/create/deploy/fix/...);
- +0.25 if spec readiness is ready/approved/complete;
- − up to 0.2 for each `missing_info` item;
- − up to 0.3 for review issues (extra penalty for blocking issues).

The score gates the low-clarity prompt and informs the cost-aware decision, but is never rendered
to the user.

---

## 8. Repository Awareness

The engine receives a `repository_snippet` built by the orchestrator from
`project_spec` (`project_type` + `features` + `framework`) — i.e. knowledge already extracted from
the repository by Requirement Discovery. `_repo_answers` normalizes the snippet; if a candidate
question's `required_field` or key term already appears in the snippet, the question is **not asked**
and is instead recorded as a safe assumption (`_repo_already_answers` + `_attach_assumption`). This
satisfies "Never ask a question already answered by the repository."

*Limitation:* the snippet is spec-derived, not a fresh deep file scan on every request (see §14).
This is intentional for cost/performance; it can be deepened in a future extension without changing
the engine.

---

## 9. Conversation Awareness

- `get_clarification_session` loads prior `questions` and `answers` from `jobs.clarification_session`.
- `prior_questions` + `existing_answers` + chat `conversation_history` (user turns) are normalized and
  unioned into `asked_or_answered`.
- Any candidate question whose text matches an already-asked or already-answered item is **dropped**
  (`_normalize_set` + membership test). This guarantees **no repeated questions** across turns and
  across channels (Telegram / Web UI / API all funnel into the same session).

On every reply, `record_clarification_answer` also mirrors the answer into workspace chat history so
the conversation is a single source of truth.

---

## 10. Assumption Strategy

When a gap cannot be asked about (repo already answers, or low-risk + safe assumption available and
within acceptable risk), an explicit assumption is generated and classified:

- `AssumptionLevel`: `CRITICAL` / `HIGH` / `MEDIUM` / `LOW` (mirrors `QuestionPriority` tiers).
- `_derive_assumptions` pulls `spec.assumptions` and `assumption_class` from questions.
- `_assumptions_within_risk` enforces that auto-continuation only happens when **every** assumption
  is at or below the configured `max_acceptable_assumption_level` (default `LOW`).
- High-risk / blocking gaps are **never** silently assumed — they force an ASK.

This satisfies "Only continue automatically when assumptions remain within acceptable risk."

---

## 11. Exception Audit

All new `except` blocks in this sprint are typed or logged:

| Location | Pattern | Verdict |
|----------|---------|---------|
| `agent_service.py:1650` | `except ClarificationSuspendSignal:` (re-raise) | ✅ typed, correct |
| `agent_service.py:1652` | `except Exception as exc:` (advisory, `logger.warning`, continue) | ✅ logged, non-critical |
| `agent_service.py:1623` | `except Exception as exc:` (persist questions, `logger.warning`) | ✅ logged |
| `agent_service.py:2271` | `except Exception as exc:` (suspend persist, `logger.warning`) | ✅ logged |
| `event_wait_engine.await_clarification_reply` | `except Exception as exc:` (answer record / job reload, `logger.warning/error`) | ✅ logged, teardown-safe |
| `interactive_wait.record_clarification_answer` | `except Exception as exc:` (load/persist/mirror, `logger.warning/error`) | ✅ logged |
| `adaptive_clarification.py` | `except Exception as exc:` (`evaluate_review`, `to_dict`) — logged | ✅ logged |

No `except Exception: pass` was introduced. No silent swallow of the suspend signal. Pre-existing
`except Exception:` blocks elsewhere in `agent_service.py` (telemetry emit, chat mirroring) are
out of scope and are non-critical third-party guards.

---

## 12. Self Audit

| Area | Finding | Status |
|------|---------|--------|
| Architecture | New decision layer injects before planning; no redesign | ✅ |
| Code duplication | Engine composes `ClarificationEngine`/`ImplementationIntelligence` rather than re-implementing | ✅ |
| Requirement flow | Completeness score + de-dup | ✅ |
| Conversation flow | History + session + chat mirror | ✅ |
| Repository analysis | Spec-derived snippet; downgrade-to-assumption | ✅ (see §14) |
| Specification flow | `project_spec` consumed | ✅ |
| Clarification quality | Quality gate drops cosmetic/low-value | ✅ |
| Resume integration | Reuses `EventWaitEngine` + `run_job` re-dispatch | ✅ |
| Event integration | `CLARIFICATION_REPLY` signal; router endpoint | ✅ |
| Implementation integration | `_implementation_report_sufficient` skip | ✅ |
| Dead code | None added | ✅ |
| Unused code | `ClarificationDecision.to_payload` kept for future persistence/observability (small, documented) | ⚠️ minor |
| Unused imports | Verified none in new module | ✅ |
| Circular imports | `ClarificationSuspendSignal` placed in `models/agent.py` (leaf) to avoid cycle | ✅ |
| Concurrency | `await_clarification_reply` parks on bus; worker released; re-dispatch via `bypass_semaphore` (same as approval) | ✅ |
| Exception handling | See §11 | ✅ |
| Maintainability | Single pure engine; orchestrator wiring localized | ✅ |
| Security | No new external input parsing beyond existing endpoints; no secrets | ✅ |
| Production readiness | Needs 3C.C's multi-node bus note (see §14) | ⚠️ |
| Backward compatibility | `ClarificationEngine` API unchanged; new field `jobs.clarification_session` (additive) | ✅ |

---

## 13. Self Fixes

1. **Async mismatch** — `ClarificationEngine.evaluate_review` is a coroutine; initial sync call
   produced a `RuntimeWarning`/empty result. Fixed by making `evaluate` and `_candidate_questions`
   `async` and `await`ing the review call. Orchestrator updated to `await`.
2. **Decision logic bug** — low-risk, non-blocking gaps always ASKed even when a safe assumption
   existed. Fixed so that when assumptions close the gap within acceptable risk, the engine returns
   `SAFE_ASSUME` (cost-aware) instead of `ASK`.
3. **Patch corruption** — the initial injected block in `agent_service.py` was written with escaped
   `\n`/`\"` sequences (tool artifact). Detected via `py_compile` failure and repaired by un-escaping
   the corrupted line in place; verified clean after.
4. **`ClarificationSuspendSignal` model placement** — added at model level (not `agent_service`) to
   prevent a circular import, mirroring the 3C.C `ApprovalSuspendSignal` pattern.

---

## 14. Remaining Limitations

1. **Repository awareness is spec-derived, not a fresh deep scan.** The `repository_snippet` comes
   from `project_spec` (architecture/features/framework), not a live file-tree read on every
   request. This prevents asking about high-level repo facts but cannot detect a deeply-buried
   constant. A future extension can deepen the snippet (bounded file read) without touching the
   engine — the input contract is already in place.
2. **Process-local event bus (inherited from 3C.C).** `EventWaitEngine` is in-memory; a clarification
   reply delivered to a different worker process will not wake the parked job in a multi-node deploy.
   Same limitation as Sprint 3C.C; resolution is a Redis pub/sub back-end for the bus.
3. **`ClarificationDecision.to_payload` is currently unused** — reserved for observability/persistence
   of the decision; harmless but flagged per the self-audit (no dead business logic).
4. **No LLM-based ambiguity scoring yet.** Completeness is heuristic. The engine is structured so an
   LLM refinement step can be added inside `evaluate` without API change; kept heuristic to avoid
   adding latency/cost on every request (consistent with the "cost-aware" goal).
5. **`implementation_report` is `None` at the pre-planning injection point** (the report is produced
   later). The skip-on-report path therefore only triggers when a caller supplies a report (e.g. a
   re-dispatch that has one); the completeness/spec path covers the common case. The integration hook
   is wired and tested.

---

## 15. Verification Performed

- **Compile:** `py_compile` on all 7 modified/new files — ✅ OK.
- **Import:** all modules import cleanly (`services.adaptive_clarification`, `agent_service`,
  `event_wait_engine`, `interactive_wait`, `routers.agents`, `models.agent`, `models.job`) — ✅ OK.
- **Unit self-test:** `sprint_3cd_self_test.py` — **8/8 PASS** (continue-on-report, ask-on-high-risk,
  repo-awareness suppression, conversation no-repeat, multi-turn suppression, low-value drop,
  safe-assume low-risk, assumption classification).
- **No polling:** clarification wake is purely event-driven (`CLARIFICATION_REPLY` / `USER_REPLY` /
  `RESUME_REQUEST`); the worker is released during the wait.

---

## 16. Production Readiness

The adaptive clarification engine is **production-ready for single-node deployments** and satisfies
all Sprint 3C.D success criteria:

- ✅ Existing architecture preserved
- ✅ Requirement Discovery reused
- ✅ Conversation reused
- ✅ Event-Driven Wait reused
- ✅ Implementation Intelligence reused
- ✅ Planner reused (unchanged)
- ✅ Repository inspected before asking (spec-derived; see limitation #1)
- ✅ Conversation inspected before asking
- ✅ Questions generated only when necessary
- ✅ No repeated questions (de-dup by history/session/turn)
- ✅ Safe assumptions supported (Critical/High/Medium/Low, risk-bounded)
- ✅ Requirement completeness evaluated (internal score)
- ✅ No duplicated logic (composition, not re-implementation)
- ✅ No silent exceptions (all `except` typed or logged)
- ✅ No dead code introduced (one reserved helper flagged)
- ✅ Modified files compile
- ✅ Self audit completed
- ✅ All fixable issues corrected

Before multi-node production, resolve limitation #2 (Redis-backed event bus) — tracked from 3C.C.
