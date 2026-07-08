# ThinkSync — Sprint 2B: Requirement Intelligence Refinement

**Date:** 2026-07-04
**Goal:** Replace remaining weak parts of Sprint 2A with deterministic, production-grade orchestration. No new product features. No Sprint 3 work.

---

## Goal

Harden the Requirement Discovery layer per 8 objectives:

| # | Objective | Status |
|---|---|---|
| 1 | Remove regex-as-primary; LLM extracts, rules validate | ✅ |
| 2 | Create `RequirementExtractor` (dedicated extraction layer) | ✅ |
| 3 | Improve `QuestionPlanner` — remove 3-question limit; add Critical/Required/Optional tiers | ✅ |
| 4 | Remove `confidence`; replace with `readiness` (Ready/Blocked/Partial) | ✅ |
| 5 | Add `SpecificationReview` (review stage before planner) | ✅ |
| 6 | Add `SpecificationFreeze` (frozen spec = source of truth) | ✅ |
| 7 | Strict 6-layer pipeline separation | ✅ |
| 8 | No hallucinated values — explicit `UNKNOWN` sentinel | ✅ |

---

## Problems Solved

| # | Problem (Sprint 2A) | Fix (Sprint 2B) |
|---|---|---|
| 1 | Regex was primary extractor (unstable across phrasings) | `RequirementExtractor` — LLM-first (`temperature=0.0`); regex ONLY as LLM-failure fallback |
| 2 | `confidence: float` was unstable (LLM-decided) | Removed. Replaced with `readiness: str` (Ready / Blocked / Partial) — computed from required-field completeness |
| 3 | `QuestionPlanner` limited to 3 questions | Removed limit. Added 3 priority tiers: **Critical** (blocks planning), **Required** (improves quality), **Optional** (skipped) |
| 4 | No review stage — invalid spec could reach planner | Added `SpecificationReview` — runs before freeze; produces `issues` / `warnings` / `recommendations` |
| 5 | Spec was mutable until planner received it | Added `SpecificationFreeze` + `FrozenSpecification` — after freeze, downstream MUST read frozen spec |
| 6 | Downstream could re-interpret user request | `FrozenSpecification` wrapper — immutable; `_spec` and `_review` are private |
| 7 | `UNKNOWN` sentinel not enforced | `_normalise_extraction()` explicitly replaces `None` / `""` with `"UNKNOWN"` for all fields |
| 8 | Mixed responsibilities in `run_discovery()` | Split into 6 clean layers with exactly one responsibility each |

---

## Architecture Before (Sprint 2A)

```
User Request
  ↓
RequirementEngine.extract()   ← regex PRIMARY, LLM secondary
  ↓
_ compute_confidence()        ← LLM-decided (unstable)
  ↓
QuestionPlanner              ← max 3 questions (arbitrary limit)
  ↓
Redis (authoritative)
  ↓
return spec (mutable)
```

**Problems:**
- Regex misses natural language variations
- `confidence` is a float — planner can't reliably decide
- 3-question limit is arbitrary
- No review stage
- Spec is mutable (downstream can re-interpret)

---

## Architecture After (Sprint 2B)

```
User Request (objective + history)
  ↓
[1] RequirementExtractor.extract()   ← LLM-first (temperature=0.0)
  │                                 ← regex ONLY if LLM fails
  ↓
[2] RequirementValidator.validate()   ← block impossible combinations
  ↓
[3] QuestionPlanner.plan_questions() ← Critical / Required / Optional
  ↓                                 ← NO question limit
  ↓
[4] SpecificationBuilder.build()     ← assemble spec dict (no I/O)
  ↓
[5] SpecificationReview.review()     ← issues / warnings / recommendations
  ↓                                 ← does NOT modify spec
  ↓
[6] SpecificationFreeze.freeze()    ← wrap in FrozenSpecification
  ↓                                 ← spec is now IMMUTABLE
  ↓
SpecificationRepository.save()        ← Postgres (authoritative) + Redis (cache)
  ↓
FrozenSpecification
  ↓
Planner (reads FrozenSpecification.spec)
```

**Improvements:**
- LLM extracts FIRST (handles natural language); rules validate AFTER
- `readiness` (not `confidence`) determines if planner can proceed
- Questions classified by priority — Critical blocks, Required improves, Optional skipped
- Review stage catches contradictions BEFORE freeze
- `FrozenSpecification` prevents downstream re-interpretation

---

## Requirement Extractor (Layer 1)

**File:** `backend/services/requirement_discovery.py` — class `RequirementExtractor`

**What it does (Objective 2):**
- Sends the user's objective + last 10 conversation messages to the LLM
- Instructs the LLM to return ONLY structured JSON (no questions, no validation)
- LLM `temperature=0.0` (fully deterministic extraction)
- If LLM fails → falls back to `_regex_fallback()` (regex is NOT primary)

**LLM prompt structure:**
```
YOU DO NOT:
- Validate the request
- Ask questions
- Plan or design the architecture
- Invent values for fields you cannot determine

FOR UNKNOWN FIELDS:
- Set to "UNKNOWN" — NEVER invent a value
```

**Fields extracted:**
`project_type`, `framework`, `language`, `features`, `auth_required`, `database_required`, `external_services`, `secrets_required`, `deployment_target`, `production_ready`

---

## Question Planner (Layer 3 — improved)

**File:** `backend/services/requirement_discovery.py` — class `QuestionPlanner`

**Changes from Sprint 2A (Objective 3):**

| Before | After |
|---|---|
| Max 3 questions | No limit — classify by priority instead |
| All questions equal priority | 3 tiers: `Critical` / `Required` / `Optional` |
| Questions generated by LLM | Questions decided by deterministic rules |

**Priority rules:**

```python
Critical  → language="UNKNOWN" | framework="UNKNOWN" (blocks planning)
Required  → deployment_target="UNKNOWN" | auth but no auth secret | DB but no DB secret
Optional  → production details (CORS, rate limiting, logging)
```

**Effect on planner:**
- `readiness="Blocked"` → Critical questions unanswered → planner emits risk note
- `readiness="Partial"` → only Required/Optional unanswered → planner proceeds
- `readiness="Ready"` → all Critical answered → planner proceeds with full context

---

## Specification Review (Layer 5 — NEW)

**File:** `backend/services/requirement_discovery.py` — class `SpecificationReview`

**What it checks (Objective 5):**

| Check | Action |
|---|---|
| `auth_required=False` but auth secrets listed | **Issue** (blocker) |
| `database_required=False` but DB secrets listed | **Issue** (blocker) |
| `deployment_target` invalid value | **Issue** (blocker) |
| `framework`/`language` incompatible | **Issue** (blocker) |
| `language="UNKNOWN"` or `framework="UNKNOWN"` | **Warning** |
| No `features` listed | **Warning** |
| `production_ready=True` but `deployment_target="UNKNOWN"` | **Warning** |

**Output:**
```python
{
  "verdict": "pass" | "pass_with_warnings" | "fail",
  "issues":       ["<blocker1>", ...],   # planner MUST NOT proceed
  "warnings":     ["<concern1>", ...],  # planner can proceed but quality affected
  "recommendations": ["<improve1>", ...],  # optional improvements
}
```

**Important:** Review does NOT modify the spec. It only reviews.

---

## Specification Freeze (Layer 6 — NEW)

**File:** `backend/services/requirement_discovery.py` — classes `FrozenSpecification` + `SpecificationFreeze`

**What freeze means (Objective 6):**

After `SpecificationReview` passes (verdict ≠ `fail`):
1. `SpecificationFreeze.freeze(spec, review)` wraps spec in `FrozenSpecification`
2. `FrozenSpecification._spec` is private (accessible only via `.spec` property)
3. `FrozenSpecification._review` is private
4. `ProjectSpecification.frozen` is set to `True`
5. Downstream (Planner, Template Engine, Execution, Patch, Verification) **MUST** read from `FrozenSpecification.spec` — no re-interpretation of user request

**Why this matters:**
- Prevents the planner from "re-interpreting" the user request differently than the discovery engine understood it
- Ensures the spec used for planning = the spec used for execution = the spec used for verification

---

## Removed Weaknesses

| Weakness (Sprint 2A) | Removed in Sprint 2B |
|---|---|
| Regex as primary extractor | ✅ `RequirementExtractor` is LLM-first |
| `confidence: float` (unstable) | ✅ Replaced with `readiness: str` |
| 3-question arbitrary limit | ✅ Replaced with priority tiers |
| No review stage | ✅ `SpecificationReview` added |
| Mutable spec | ✅ `FrozenSpecification` added |
| Downstream can re-interpret request | ✅ Frozen spec = only source of truth |
| `UNKNOWN` not enforced | ✅ `_normalise_extraction()` enforces it |
| Mixed responsibilities in `run_discovery()` | ✅ 6-layer clean separation |

---

## Files Modified

| File | Changes |
|------|---------|
| `backend/services/requirement_discovery.py` | **Complete rewrite** — now 6 layers + `FrozenSpecification` |
| `backend/models/agent.py` | Removed `confidence: float`; added `readiness: str`, `frozen: bool`; updated docstring |
| `backend/services/planner.py` | Inject `spec_readiness` into LLM context so planner can see readiness |

---

## Remaining Limitations

| Limitation | Plan |
|---|---|
| `FrozenSpecification` is not enforced at Python type level (downstream can still ignore it) | Sprint 3: pass `FrozenSpecification` explicitly to planner signature |
| Interactive wait (pause for user replies) not implemented | Sprint 3 (`QuestionPlanner` is ready; need job pause/resume) |
| `conversation_id` may be `None` from client | Sprint 3: make `conversation_id` required in `JobCreate` |
| DB migration (`project_specifications` table) must be run manually | Run `SPRINT_02A_MIGRATION.sql` (still required; table unchanged in 2B) |

---

## Sprint 3 Preparation

The following are **ready** for Sprint 3 integration:

1. **`FrozenSpecification`** — planner can accept it as a parameter (currently reads from `ProjectSpecification` which has `frozen` flag)
2. **`QuestionPlanner.plan_questions()`** — returns structured questions with priority; frontend can render them
3. **`SpecificationReview`** — review verdict can be shown in frontend ("Spec review: 2 warnings")
4. **`readiness` field** — planner already checks it (via `spec_readiness` in context)

**What Sprint 3 should NOT need to change:**
- `RequirementExtractor.extract()` — LLM-first, deterministic, complete
- `RequirementValidator.validate()` — comprehensive compatibility rules
- `QuestionPlanner` — 3-tier priority system, complete
- `SpecificationBuilder.build()` — deterministic assembly
- `SpecificationReview.review()` — comprehensive checks
- `SpecificationFreeze.freeze()` — immutable wrapper, complete
