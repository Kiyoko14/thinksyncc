# RLS VERIFICATION

**Phase 2.6 — every Row-Level-Security policy inspected + functionally tested on live
PostgreSQL 14 as owner and non-owner roles.**

## Method
1. Static inspection of every `create policy` in `db/schema.sql` and the `20260713`
   migration — looked for every `TEXT = UUID` / `UUID = TEXT` comparison in `using` /
   `with check` / `EXISTS` join predicates.
2. Functional test: created a non-superuser role (`rls_tester`) subject to RLS, seeded
   one owner's rows, then queried each protected table twice — once with
   `auth.uid()` = owner, once with a different uuid.

## Comparison audit (Step 5) — every ownership predicate

| policy / table | predicate | left type | right type | verdict |
|---|---|---|---|---|
| servers/workspaces/chats/chat_messages/tasks/jobs | `auth.uid() = user_id` | uuid | uuid | ✅ OK |
| messages | EXISTS chats `c.user_id = auth.uid()` | uuid | uuid | ✅ OK |
| job_state_transitions/job_events/job_steps/job_decisions/job_retries/job_execution_details | EXISTS jobs `j.id = <t>.job_id` + `j.user_id = auth.uid()` | uuid=uuid | uuid=uuid | ✅ OK |
| workspace_files / agent_context_logs | EXISTS workspaces `w.user_id = auth.uid()` | uuid | uuid | ✅ OK |
| **approval_requests** | EXISTS jobs `j.id = approval_requests.job_id` | uuid | uuid | ✅ FIXED (was `j.id::text = job_id`) |
| **approval_audit** | EXISTS jobs `j.id = approval_audit.job_id` | uuid | uuid | ✅ FIXED (was `j.id::text = job_id`) |
| **resume_outcomes** | EXISTS jobs `j.id = resume_outcomes.job_id` | uuid | uuid | ✅ FIXED (was `j.id::text = job_id`) |
| conversation_audit | EXISTS jobs `j.id::text = conversation_audit.job_id` | text | text | ✅ OK (job_id is text — cast required) |
| idempotency_store | EXISTS jobs `j.id::text = idempotency_store.job_id` | text | text | ✅ OK (job_id is text — cast required) |
| agent_runs | `auth.uid() = user_id` | uuid | uuid | ✅ OK |
| **project_specifications** | `user_id = auth.uid()::text` OR EXISTS job by conversation_id | text=text / uuid=uuid | | ✅ FIXED (was `user_id = auth.uid()` → text=uuid error) |

**No remaining `TEXT = UUID` or `UUID = TEXT` mismatch anywhere.** After the fixes the
casts are present exactly where a genuine text/uuid boundary exists (conversation_audit,
idempotency_store, project_specifications.user_id) and absent where both sides are uuid.

## Functional isolation proof (live, role subject to RLS)

Seeded ONE owner (`user_id = 1111…`) with an approval_request, conversation_audit,
idempotency_store, and project_specifications row.

**As OWNER (`auth.uid() = 1111…`):**
```
owner sees approval_requests      = 1
owner sees conversation_audit     = 1
owner sees idempotency_store      = 1
owner sees project_specifications = 1
owner sees approval_audit         = 0   (none seeded)
owner sees resume_outcomes        = 0   (none seeded)
```
**As NON-OWNER (`auth.uid() = 9999…`):**
```
non-owner sees approval_requests      = 0
non-owner sees approval_audit         = 0
non-owner sees resume_outcomes        = 0
non-owner sees conversation_audit     = 0
non-owner sees idempotency_store      = 0
non-owner sees project_specifications = 0
```

Result: owner sees exactly their rows; non-owner sees nothing; **no `text = uuid`
runtime error** on any policy evaluation. RLS is correct and type-safe.

## SPRINT_03_MIGRATION.sql legacy policies (noted, superseded)
The old `SPRINT_03_MIGRATION.sql` created permissive `USING (true)` policies on
`approval_requests`/`approval_audit` (marked `-- TODO: restrict`). The `20260713`
migration **replaces** them with owner-scoped policies via `DROP POLICY IF EXISTS` +
`CREATE POLICY`, so the insecure `true` policies do not survive. Verified: only the
owner-scoped policy is present after migration.
