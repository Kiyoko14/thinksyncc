# ThinkSync — Sprint 2A: Requirement Discovery Hardening

**Date:** 2026-07-04
**Goal:** Convert the LLM-controlled discovery into a deterministic orchestration layer. No new product features. No Sprint 3 work.

---

## Goal

Harden the Requirement Discovery layer created in Sprint 2:

1. Remove LLM control over **what questions to ask** (now rule-based via `QuestionPlanner`)
2. Add `RequirementValidator` — reject impossible combinations (Python+Spring, Node+Django)
3. Add `SpecificationValidator` — final gate before planner receives the spec
4. Separate persistence (Postgres authoritative) from caching (Redis-only)
5. Create clean interfaces reusable in Sprint 3
6. Improve determinism (fixed rules, low-temperature LLM only for feature summary)
7. Never guess secrets, frameworks, or deployment targets

---

## Problems Fixed

| # | Problem (Sprint 2) | Fix (Sprint 2A) |
|---|---|---|
| 1 | LLM decides what questions to ask | `QuestionPlanner` — deterministic rules, max 3 questions |
| 2 | No validation of framework/language compatibility | `RequirementValidator.validate()` — blocklist of incompatible pairs |
| 3 | Invalid spec can reach the planner | `SpecificationValidator.validate()` — gate before planner |
| 4 | Redis is authoritative (losing Redis = losing spec) | `SpecificationRepository` — Postgres authoritative, Redis = cache |
| 5 | LLM prompt has high temperature (unstable output) | `temperature=0.1` for feature summary; all other fields = rule-based |
| 6 | `confidence` computation is inside the LLM prompt (unstable) | `_compute_confidence()` — deterministic formula |
| 7 | `missing_info` computed by LLM (unstable) | `_compute_missing_info()` — rule-based |
| 8 | No check for `production_ready=True` + `deployment_target=unknown` | `RequirementValidator` catches this |
| 9 | No check for `auth_required=True` + no auth secrets | `RequirementValidator` catches this |

---

## Architecture Before

```
User Request
  ↓
run_discovery()
  ↓
LLM call (_INITIAL_SPEC_PROMPT)
  ↓                    ← LLM controls:
  │                      - what fields to extract
  - what questions to ask  ← LLM decides
  - confidence score        ← LLM decides
  - missing_info           ← LLM decides
  ↓
Redis (authoritative)   ← REDIS_SPEC_KEY
  ↓
return spec
```

**Problems:**
- LLM decides too much (questions, confidence, missing_info)
- Redis is authoritative (losing Redis = data loss)
- No validation of impossible combinations
- Non-deterministic (same input → different output)

---

## Architecture After

```
User Request
  ↓
run_discovery()
  ↓
RequirementEngine.extract()   ← RULE-BASED (no LLM)
  │                           - keyword regex matching
  │                           - deterministic field extraction
  ↓
RequirementValidator.validate() ← COMPATIBILITY CHECK
  │                           - Python+Spring? REJECT
  │                           - Node+Django? REJECT
  ↓
SpecificationValidator.validate() ← FINAL GATE
  │                             - confidence ≥ 0.3
  │                             - framework OR language known
  │                             - no impossible combinations
  ↓ (if valid)
QuestionPlanner.decide_questions() ← RULE-BASED (no LLM)
  │                                 - priority order: language → framework → deployment → secrets
  │                                 - max 3 questions
  ↓
SpecificationRepository.save() ← POSTGRES + Redis cache
  │
  ├── Postgres:  project_specifications table (authoritative)
  └── Redis:      project_spec:{conversation_id}  (cache, TTL 6h)
  ↓
return spec
```

**Improvements:**
- LLM only summarises features (1 call, `temperature=0.1`)
- All other fields = deterministic regex/rule matching
- Impossible combinations blocked before planner sees them
- Postgres is authoritative (Redis loss = no data loss)
- Identical inputs → identical spec (deterministic)

---

## Requirement Engine

**File:** `backend/services/requirement_discovery.py` — class `RequirementEngine`

**What it does:**
- Uses regex keyword matching to extract: `project_type`, `framework`, `language`, `auth_required`, `database_required`, `production_ready`, `deployment_target`, `external_services`, `secrets_required`
- Only uses LLM for: `_summary_features()` (feature bullet-point summary)
- LLM instruction explicitly forbids: asking questions, deciding missing info, inventing values

**Key regex rules:**
- `project_type`: matches `web_app`, `api`, `bot`, `cli`, `library`, `ml_model`
- `framework`: matches `fastapi`, `flask`, `django`, `spring boot`, `express`, `next.js`, `nestjs`, `gin`, `actix`
- `language`: matches `python`, `javascript`, `typescript`, `java`, `go`, `rust`
- `deployment_target`: only set if user explicitly mentions `localhost` / `thinkync` / `cloud` — otherwise `"unknown"`

---

## Requirement Validator

**File:** `backend/services/requirement_discovery.py` — class `RequirementValidator`

**What it rejects:**

```python
# Example impossible combinations
(language="python",     framework="spring_boot")  → REJECT
(language="javascript",  framework="django")        → REJECT
(language="java",        framework="express")        → REJECT
(language="python",     framework="next.js")        → REJECT

# Example policy violations
(auth_required=True,  no auth secrets listed)      → WARNING
(database_required=True, no DB secrets listed)      → WARNING
(production_ready=True, deployment_target="unknown") → WARNING
```

**Allow-list architecture:**
- `_COMPATIBLE`: dict of `language → set[frameworks]`
- `_FRAMEWORK_LANG`: inverse dict `framework → language`
- If `framework` is known but `language` doesn't match → reject

---

## Specification Validator

**File:** `backend/services/requirement_discovery.py` — class `SpecificationValidator`

**Gate checks (must ALL pass for spec to reach planner):**

| Check | Condition | Failure action |
|---|---|---|
| Compatibility | `RequirementValidator.validate()` passes | Auto-fix: set `framework="unknown"`, add to `missing_info` |
| Minimum confidence | `confidence ≥ 0.3` | Set `confidence=0.2`, add to `missing_info` |
| Minimum info | `framework != "unknown"` OR `language != "unknown"` | Block planning; attach error to spec |

---

## Specification Persistence

**Before:** Redis key `req_discovery:spec:{conversation_id}` — **authoritative** (losing Redis = losing spec)

**After:**

```
Postgres table: project_specifications
─────────────────────────────────────────
id                uuid PK
conversation_id   text UNIQUE   ← query key
user_id           text
spec_json         jsonb          ← full ProjectSpecification
created_at        timestamptz
updated_at        timestamptz

Redis key: project_spec:{conversation_id}  (TTL=6h, CACHE ONLY)
```

**Read path:**
1. Try Redis → hit: return immediately
2. Redis miss → read Postgres → hit: return + re-populate Redis
3. Both miss → run discovery

**Write path:**
1. Upsert Postgres (`INSERT ... ON CONFLICT(conversation_id) UPDATE ...`)
2. Update Redis cache

**Redis loss scenario:** Spec is still in Postgres. Next read hits Postgres and re-populates Redis. No data loss.

---

## Redis Changes

| Key (Before) | Key (After) | Purpose |
|---|---|---|
| `req_discovery:spec:{cid}` | `project_spec:{cid}` | Cache only (not authoritative) |
| — | `job:spec:{job_id}` (future) | Per-job spec override |

**Backward compatibility:** The old Redis key format is no longer written. `get_cached_spec()` reads the new key. Old keys are ignored (they expire after 6h TTL).

---

## Files Modified

| File | Changes |
|------|---------|
| `backend/services/requirement_discovery.py` | **COMPLETE REWRITE** — now contains `RequirementEngine`, `RequirementValidator`, `SpecificationValidator`, `SpecificationRepository`, `QuestionPlanner` |
| `SPRINT_02A_MIGRATION.sql` | **NEW FILE** — DB migration to create `project_specifications` table and add `specification` column to `jobs` |

**Files NOT modified (backward compatible):**
- `backend/services/agent_service.py` — unchanged; `run_discovery()` has same signature
- `backend/services/planner.py` — unchanged; receives `project_spec` as before
- `backend/models/agent.py` — unchanged; `ProjectSpecification` model is the same

---

## Remaining Limitations

| Limitation | Plan |
|---|---|
| DB migration (`project_specifications` table) must be run manually in Supabase before deploying | Run `SPRINT_02A_MIGRATION.sql` in Supabase SQL editor |
| `SpecificationRepository` uses Supabase client (sync) inside `async def save()` — may block event loop on slow DB | Wrap in `loop.run_in_executor()` in Sprint 3 |
| Question/answer loop is NOT implemented (questions are listed in `missing_info` but not sent to user interactively) | Sprint 3 (Interactive Wait) |
| `conversation_id` may be `None` if client doesn't send it — falls back to `job_id` (per-job spec, not per-conversation) | Sprint 3 (Client API update) |
| Feature summary still uses LLM (though only 1 call, low temp) | Could be replaced with pure rule-based noun-phrase extraction in Sprint 3 |

---

## Sprint 3 Preparation

The following interfaces are now in place and ready for Sprint 3:

1. **`SpecificationRepository`** — ready for interactive wait (Sprint 3 can call `save()` after user replies)
2. **`QuestionPlanner`** — ready to emit structured questions to the frontend (currently only populates `missing_info`; Sprint 3 can emit events)
3. **`SpecificationValidator`** — ready as a gate before any planning
4. **`RequirementValidator`** — can be extended with more compatibility rules
5. **DB table `project_specifications`** — authoritative storage; Sprint 3 can add `project_id` FK when Project model is added

**What Sprint 3 should NOT need to change:**
- `RequirementEngine.extract()` — deterministic, complete
- `QuestionPlanner.decide_questions()` — rule-based, complete
- `SpecificationRepository` interface — stable

**What Sprint 3 will add:**
- Interactive wait (pause job after discovery, wait for user replies, then re-run `SpecificationRepository.save()`)
- Frontend component to display `QuestionPlanner` questions
- `conversation_id` in `JobCreate` from the client (currently optional)
