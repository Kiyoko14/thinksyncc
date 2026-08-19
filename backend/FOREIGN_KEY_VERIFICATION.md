# FOREIGN KEY VERIFICATION

**Phase 2.6 — every FK proven creatable + type-identical on live PostgreSQL 14.**

## Method
The fixed schema was applied to a clean database and every foreign key was queried from
`pg_constraint`. All 28 FKs were created without error. Below are the ones touched or
verified for the Sprint 3 contract.

## FK inventory (post-fix, live `pg_get_constraintdef`)

| child table | FK column | → referenced | ref type | child type | creatable? |
|---|---|---|---|---|---|
| approval_requests | job_id | jobs(id) | uuid | **uuid** | ✅ yes |
| approval_audit | job_id | jobs(id) | uuid | **uuid** | ✅ yes |
| approval_audit | approval_id | approval_requests(approval_id) | text | text | ✅ yes |
| resume_outcomes | job_id | jobs(id) | uuid | **uuid** | ✅ yes |
| worker_heartbeats | job_id | jobs(id) | uuid | uuid | ✅ yes (ON DELETE SET NULL) |
| workspace_deployments | workspace_id | workspaces(id) | uuid | uuid | ✅ yes |
| job_state_transitions | job_id | jobs(id) | uuid | uuid | ✅ yes |
| job_events | job_id | jobs(id) | uuid | uuid | ✅ yes |
| job_steps | job_id | jobs(id) | uuid | uuid | ✅ yes |
| job_decisions | job_id | jobs(id) | uuid | uuid | ✅ yes |
| job_retries | job_id | jobs(id) | uuid | uuid | ✅ yes |
| job_execution_details | job_id | jobs(id) | uuid | uuid | ✅ yes |
| jobs | user_id | auth.users(id) | uuid | uuid | ✅ yes |
| jobs | workspace_id | workspaces(id) | uuid | uuid | ✅ yes |
| jobs | server_id | servers(id) | uuid | uuid | ✅ yes |
| workspaces | server_id / user_id | servers/auth.users | uuid | uuid | ✅ yes |

Live `pg_get_constraintdef` for the three FIXED constraints:
```
approval_requests_job_id_fkey  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
approval_audit_job_id_fkey     FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
resume_outcomes_job_id_fkey    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
```

## Tables WITHOUT a `job_id` FK (by design — verified correct)

| table | job_id type | why no FK |
|---|---|---|
| conversation_audit | text | `AuditEvent.job_id` persisted via `model_dump_json()`; stored as text; ownership enforced by RLS `j.id::text = job_id` |
| idempotency_store | text | `IdempotencyStore.record` writes job_id as text; ownership via RLS `j.id::text = job_id` |

These are intentional: they store the UUID as text and never declare an FK, so there is
no type-identity requirement. Their RLS predicates cast `jobs.id::text` — verified valid.

## Referential-integrity functional proof (live)
- Inserted a real job (`id = 33333333-…` uuid) + a child `approval_requests` row whose
  `job_id` = that uuid, `approval_id` = `a1b2c3d4e5f60718` (16-hex text). **INSERT 0 1** ✅
- Inserted matching `approval_audit`, `resume_outcomes`, `conversation_audit`,
  `idempotency_store` rows. **All succeeded.** ✅
- `DELETE FROM jobs WHERE id = 33333333-…` → **cascaded**: `approval_requests` count
  dropped to **0** via `ON DELETE CASCADE`. ✅

Every foreign key in the schema is valid and enforceable. Zero FK violations remain.
