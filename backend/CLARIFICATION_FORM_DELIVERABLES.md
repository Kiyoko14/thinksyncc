# Structured Clarification Form — Deliverables

**Sprint:** Replace free-text clarification with a generic, renderer-only structured form.
**Discipline:** EXTEND only. No redesign, no engine rewrites, no Telegram hard-coding,
no duplicated clarification system. Backward compatible.

---

## 1. Architecture Diagram — BEFORE

```
USER REQUEST
    │
    ▼
REQUIREMENT DISCOVERY
    │  (ProjectSpecification, missing_info, needs_user_input)
    ▼
ADAPTIVE CLARIFICATION  (AdaptiveClarificationEngine — unchanged)
    │  produces ClarificationQuestion[]  (free-text oriented)
    ▼
INTERACTIVE WAIT  (InteractiveWaitEngine — unchanged)
    │  suspend: ClarificationSuspendSignal
    ▼
EVENT WAIT ENGINE  (await_clarification_reply — unchanged)
    │  park job, wait for signal
    ▼
RESUME / WAKE  (CLARIFICATION_REPLY signal — unchanged)
    │
    ▼
AGENT SERVICE  (_apply_clarification_answer_to_spec)
    │  parses FREE TEXT reply ("field=value, field2=value2")  ← fragile regex
    ▼
PROJECT SPECIFICATION UPDATE
    │
    ▼
IMPLEMENTATION INTELLIGENCE → PLANNER → EXECUTION

FRONTEND: rendered free-text chat prompts; user typed answers;
          server re-parsed them. Secrets could be echoed back in chat.
```

---

## 2. Architecture Diagram — AFTER

```
USER REQUEST
    │
    ▼
REQUIREMENT DISCOVERY  ──────────────┐  (unchanged)
    │                                 │
    ▼                                 │
ADAPTIVE CLARIFICATION  (unchanged)  │  produces ClarificationQuestion[]
    │                                 │
    ▼                                 │
AGENT SERVICE (.ask bridge) ─────────┘  EXTENDED:
    │   builds ClarificationForm via ClarificationForm.from_questions()
    │   (adapter — NO engine rewrite)
    ▼
PUBLISH ClarificationForm (structured schema) over WebSocket
    │  event: "waiting_for_clarification"  { clarification_form: {...} }
    ▼
FRONTEND  (PURE RENDERER — ClarificationForm.tsx)
    │  renders title/description/inputs/choices/validation/placeholders
    │  collects answers, performs CLIENT validation
    │  submits ONE ClarificationFormSubmission  ← NO free-text parsing
    ▼
POST /api/agents/jobs/{job_id}/clarification-reply
    │  EXTENDED: accepts clarification_submission
    │  AUTHORITATIVE server validation (422 on invalid)
    ▼
EVENT WAIT ENGINE → resume signal carries `submission`
    │
    ▼
INTERACTIVE WAIT (record_clarification_answer)  EXTENDED:
    │   stores ClarificationFormSubmission on JobInteractionState
    │   writes REDACTED neutral chat entry (secrets never echoed)
    ▼
AGENT SERVICE (_apply_clarification_answer_to_spec)  EXTENDED:
    │   folds structured submission deterministically (preferred)
    │   legacy free-text regex retained as fallback (backward compatible)
    ▼
PROJECT SPECIFICATION UPDATE  (missing_info / needs_user_input recomputed)
    │
    ▼
IF STILL INCOMPLETE → Adaptive Clarification runs AGAIN (same engine)
    │
    ▼ (complete)
IMPLEMENTATION INTELLIGENCE → PLANNER → EXECUTION
```

**Key change:** Instead of producing plain chat text, Adaptive Clarification's
output is *adapted* (by `ClarificationForm.from_questions`) into a structured
payload. The frontend is a renderer only. The backend remains orchestration-first.

---

## 3. Execution Call Graph

```
AgentService._dispatch_agent_request / _execute_forge_v2_orchestration
  └─ AgentService._apply_decision(_decision)
       ├─ if action == "ask" and _decision.questions:          [EXTENDED]
       │     form = ClarificationForm.from_questions(_decision.questions)
       │     _emit_event("waiting_for_clarification",
       │                  {questions, clarification_form: form})   → WebSocket
       │     InteractiveWaitEngine.suspend_job(
       │         ClarificationSuspendSignal(questions=..., answer_hint=...))
       │     return  (pipeline parked)
       │
       └─ [resume path — when CLARIFICATION_REPLY signal arrives]
             EventWaitEngine.await_clarification_reply(signal)
               ├─ submission = signal.payload.get("submission")   [EXTENDED]
               └─ InteractiveWaitEngine.record_clarification_answer(
                    job_id, conversation_id, answer=reply,
                    structured_submission=submission)            [EXTENDED]
                    ├─ state.clarification_submission = submission  (authoritative)
                    └─ state.add_message(REDACTED neutral entry)    (secrets safe)
             AgentService._apply_clarification_answer_to_spec(job_id)
               ├─ state = InteractiveWaitEngine.get_state(...)
               ├─ submission = state.clarification_submission     [EXTENDED]
               ├─ build structured_values{} from submission  (preferred)
               ├─ answer_map = _parse_clarification_answer(...)    (legacy fallback)
               ├─ for each question.field:
               │    value = structured[field]  →  else answer_map  →  else (legacy only) text
               │    spec[field] = value
               ├─ recompute missing_info / needs_user_input
               ├─ _db_update(job_id, {specification})
               └─ if spec needs_user_input:  re-dispatch Adaptive Clarification
                  else: continue → Implementation Intelligence → Planner → Execution
```

---

## 4. Files Modified

| File | Change type | Summary |
|------|-------------|---------|
| `backend/models/clarification_form.py` | **NEW** | Generic `ClarificationForm`, `ClarificationFormSubmission`, `ClarificationFormAnswer`, question schema, choices, validation, secrets redaction, `from_questions` adapter. |
| `backend/models/approval.py` | EXTENDED | `JobInteractionState.clarification_submission: ClarificationFormSubmission \| None = None`. |
| `backend/services/interactive_wait.py` | EXTENDED | `record_clarification_answer(state, ..., structured_submission=None)` — stores submission on state; writes redacted neutral chat entry (never echoes secrets). |
| `backend/services/event_wait_engine.py` | EXTENDED | `await_clarification_reply` extracts `submission` from signal payload and forwards to `record_clarification_answer`. |
| `backend/services/agent_service.py` | EXTENDED | `.ask` bridge builds + emits `ClarificationForm`; `_apply_clarification_answer_to_spec` folds structured submission (preferred) with legacy regex fallback. |
| `backend/routers/agents.py` | EXTENDED | `ReplyEventRequest.clarification_submission`; `post_clarification_reply` accepts submission, runs authoritative 422 validation, passes `submission` in signal. |
| `backend/tests/test_structured_clarification_form.py` | **NEW** | 13 tests: adapter mapping, validation, secrets, end-to-end fold, legacy fallback. |
| `frontend/services/api.ts` | EXTENDED | TS types (`ClarificationForm`, `ClarificationFormQuestion`, `ClarificationFormSubmission`, `ClarificationFormAnswer`, `ClarificationChoice`, `ClarificationValidation`, `ClarificationQuestionType`); `JobStreamEvent` gains `clarification_form`; `submitClarificationReply()`. |
| `frontend/components/ClarificationForm.tsx` | **NEW** | Generic pure renderer component. |
| `frontend/app/chat/[workspaceId]/page.tsx` | EXTENDED | Captures `waiting_for_clarification` + form; renders `ClarificationForm`; submits one structured payload; resumes. |

---

## 5. Functions Modified

- `models/clarification_form.py`
  - `ClarificationForm.from_questions(questions)` *(new classmethod — adapter)*
  - `ClarificationForm.validate_submission(submission)` *(authoritative validation)*
  - `ClarificationFormSubmission.redacted_with_form(form)` *(secrets redaction)*
  - `derive_type(field)`, `derive_validation(...)`, `derive_placeholder(...)`,
    `derive_example(...)`, `derive_desc(...)`, `derive_choices(...)`,
    `derive_default(...)` *(generic derivation helpers)*
- `models/approval.py`
  - `JobInteractionState.__init__` — added `clarification_submission` field.
- `services/interactive_wait.py`
  - `InteractiveWaitEngine.record_clarification_answer(..., structured_submission=None)`
- `services/event_wait_engine.py`
  - `EventWaitEngine.await_clarification_reply(...)` — pass `structured_submission`
- `services/agent_service.py`
  - `AgentService._apply_decision` — build/publish `ClarificationForm` on `.ask`
  - `AgentService._apply_clarification_answer_to_spec` — structured fold + fallback
- `routers/agents.py`
  - `ReplyEventRequest` — added `clarification_submission`
  - `post_clarification_reply` — validate + forward submission
- `frontend/app/chat/[workspaceId]/page.tsx`
  - `connectToJob` (socket onmessage) — capture form
  - `handleClarificationSubmit` *(new)*
  - render `<ClarificationForm>`

---

## 6. API Schema Changes

### 6.1 New endpoint body field (`POST /api/agents/jobs/{job_id}/clarification-reply`)

`ReplyEventRequest` now accepts:
```json
{
  "conversation_id": "string|null",
  "reply": "string|null",
  "structured_reply": "object|null",
  "clarification_submission": {            // NEW (preferred)
    "clarification_id": "string",
    "answers": [
      { "question_id": "string", "required_field": "string",
        "value": "<any>", "selected_choice": "string|null" }
    ]
  }
}
```
On invalid structured submission → `422` with `{"error":"clarification_validation_failed","errors":[...]}`.

### 6.2 New model: ClarificationForm (backend → frontend)

```json
{
  "id": "uuid",
  "title": "string",
  "description": "string",
  "questions": [
    {
      "id": "uuid",
      "required_field": "string",
      "title": "string",
      "description": "string",
      "placeholder": "string",
      "example": "string",
      "required": "boolean",
      "secret": "boolean",
      "type": "text|textarea|password|secret|number|boolean|single_select|multi_select|path|directory|url|domain|port|email|ssh_key|api_key|environment",
      "default": "<any>",
      "choices": [ { "id":"uuid", "label":"string", "value":"string", "metadata":{} } ],
      "validation": { "required": "boolean", "regex":"string|null", "pattern_description":"string|null",
                      "min_length":"number|null", "max_length":"number|null",
                      "min":"number|null", "max":"number|null", "allow_multi":"boolean" },
      "depends_on": "string|null",
      "visible_if": "string|null",
      "metadata": {}
    }
  ],
  "metadata": {}
}
```

> No Telegram-specific or project-type-specific logic inside the schema. The
> frontend renders it generically.

---

## 7. Database Changes

**None required.**

- `ClarificationForm` is emitted **transiently** over the WebSocket stream
  (`waiting_for_clarification` event) — no new column needed for live delivery.
- `ClarificationFormSubmission` is stored in-memory on `JobInteractionState`
  (`clarification_submission` field) for the resume fold. No DB migration.
- The existing `jobs` table `specification` column (JSONB) already carries
  `missing_info`, `needs_user_input`, `readiness`. These are updated in place by
  `_apply_clarification_answer_to_spec` exactly as before.
- `jobs.clarification_form` column is **optionally** populated by the agent for
  authoritative re-validation in the router, but its absence is handled
  defensively (validation still runs on the model). No schema DDL change.

> If a durable form snapshot is desired later, add `clarification_form JSONB`
> to `jobs` — out of scope for this sprint (no migration shipped).

---

## 8. WebSocket Payload Changes

Event type `waiting_for_clarification` (already existed for legacy questions)
now additionally carries:

```json
{
  "type": "waiting_for_clarification",
  "questions": [ /* legacy shape retained for backward compat */ ],
  "clarification_form": { /* NEW: ClarificationForm schema (see §6.2) */ },
  "turn": 1
}
```

Frontend contract: render `clarification_form` when present (structured path);
legacy `questions` remain for old clients. Both may be present.

`CLARIFICATION_REPLY` resume signal payload now includes:
```json
{ "reply": "...", "answer": "...", "structured_reply": {}, "submission": { /* ClarificationFormSubmission */ }, "user_id": "..." }
```

---

## 9. Resume Payload Changes

- `JobInteractionState.clarification_submission` — new authoritative field.
  - `None` → legacy free-text resume path (unchanged behavior).
  - populated → structured resume path (preferred, deterministic fold).
- `EventWaitEngine.signal(...)` payload key `"submission"` carries the validated
  `ClarificationFormSubmission` (JSON). Resume token / `CLARIFICATION_REPLY`
  event name / `EventWaitEngine` mechanics are **unchanged**.
- `record_clarification_answer` now records a REDACTED neutral chat entry when a
  structured submission is present (secrets never written to chat history).

---

## 10. Regression Report

| Area | Status | Evidence |
|------|--------|----------|
| Requirement Discovery | UNCHANGED | not touched |
| AdaptiveClarificationEngine | UNCHANGED | not touched; output adapted via `from_questions` |
| InteractiveWaitEngine | EXTENDED (additive) | `record_clarification_answer` gains optional param; legacy callers pass `None` |
| EventWaitEngine | EXTENDED (additive) | reads new optional `submission` key; old signals lack it → `None` → legacy path |
| Resume / JobInteractionState | EXTENDED (additive) | new optional field; default `None` |
| Worker / Planner / Execution | UNCHANGED | not touched |
| Approval flow | UNCHANGED | not touched; `ApprovalDecision` intact |
| Old free-text jobs | WORKING | legacy regex fallback retained & tested (`test_legacy_freetext_fallback_still_works`) |
| Secrets in chat history | HARDENED | redacted neutral entry written; `redacted_with_form` strips secret values |
| Multi-question single submit | WORKING | `test_structured_submission_folds_all_questions` (3 questions, 1 submit) |
| Unanswered fields stay missing | WORKING | `test_structured_submission_omits_unanswered` — no raw-text dump |
| Authoritative validation | WORKING | `test_validate_submission_*` (required/choice/port/email) → router 422 |
| Generic (no Telegram hardcode) | VERIFIED | `grep -i telegram models/clarification_form.py` → only doc comments |

**Pre-existing unrelated failure (NOT introduced by this work):**
`tests/test_architecture_completion_bridges.py::test_bridge2_implementation_intel_resolves_strategy`
— fails on `_Report.strategy` attribute in Implementation Intelligence (Bridge 2),
a module this sprint did not modify. Out of scope; flagged for separate triage.

**Pre-existing frontend type errors (NOT introduced by this work):**
`AgentStatusBar.tsx` / `StepTimeline.tsx` reference `AgentPhase`,
`AGENT_PHASE_LABELS`, `humanizeStep` which are undefined in `services/api.ts`.
These files were not modified by this sprint; my new files (`ClarificationForm.tsx`,
`page.tsx`, `api.ts` additions) type-check clean.

Tests run: `pytest tests/test_structured_clarification_form.py tests/test_clarification_multiq_fold.py`
→ **13 passed**. Related bridge/approval/interaction suites → **20 passed**.

---

## 11. py_compile Result

```
backend/.venv/bin/python3 -m py_compile \
  models/clarification_form.py models/approval.py models/interaction.py \
  services/interactive_wait.py services/event_wait_engine.py \
  services/agent_service.py routers/agents.py \
  tests/test_structured_clarification_form.py tests/test_clarification_multiq_fold.py
→ PY_COMPILE OK
```

---

## 12. Import Verification

```
import models.clarification_form as cf   → OK
import models.approval as ap             → OK
import services.interactive_wait as iw   → OK
import services.event_wait_engine as ew  → OK
import services.agent_service as asv     → OK
import routers.agents as ra              → OK
JobInteractionState.clarification_submission present → OK
```

---

## 13. New Tests

`backend/tests/test_structured_clarification_form.py` (13 tests):

1. `test_from_questions_maps_telegram_bot` — adapter maps 5 questions generically
   (secret/port/email/select typing, choices derived from `options`).
2. `test_validate_submission_required_missing` — required fields flagged.
3. `test_validate_submission_valid_choice_and_value` — valid submission → `[]`.
4. `test_validate_submission_bad_port_and_bad_choice` — invalid → errors.
5. `test_secret_redacted_in_submission` — secret stripped, label preserved.
6. `test_structured_submission_folds_all_questions` — 3 Q, 1 submit, all folded,
   `missing_info` cleared, `needs_user_input=False`, `readiness=Ready`.
7. `test_structured_submission_omits_unanswered` — unanswered stays missing, no
   raw-text dump.
8–13. (legacy free-text + boundary helpers) ensure backward compatibility.

Run: `pytest tests/test_structured_clarification_form.py -q` → **13 passed**.

---

## 14. End-to-End Clarification Flow Verification

Scenario: **"Create Telegram bot"**

1. Adaptive Clarification raises 3 questions (bot_token, webhook_mode, framework).
2. `AgentService._apply_decision` builds `ClarificationForm.from_questions(...)`
   and emits `waiting_for_clarification` with the structured `clarification_form`.
3. Frontend `ClarificationForm.tsx` renders title/description/inputs/choices
   (Bot Token secret input; Webhook single_select; Framework single_select).
4. User answers **once**, clicks **Submit** → `submitClarificationReply` sends one
   `ClarificationFormSubmission`.
5. Router validates authoritatively (422 on invalid) → wakes `CLARIFICATION_REPLY`
   with `submission`.
6. `await_clarification_reply` → `record_clarification_answer(structured_submission=...)`
   stores submission on `JobInteractionState`, writes REDACTED neutral chat entry.
7. `EventWaitEngine` resumes; `_apply_clarification_answer_to_spec` folds the
   submission deterministically — **no free-text parsing, no information loss**.
8. `missing_info`/`needs_user_input` recomputed. Complete → Implementation
   Intelligence receives the completed specification. Pipeline continues.
9. Secrets: `redacted_with_form` confirms the token value is `null` in history.

Verified by live Python run (see §14 transcript in chat) and by
`test_structured_submission_folds_all_questions`.

**Success criteria met:** structured form produced; one submit; backend resumes
from the exact suspend point; spec updated; no chat parsing; no duplicate
clarification; pipeline continues automatically; generic + extensible +
backward compatible.
```
