-- ThinkSync: production-grade workspace (project) model
-- Enforces deterministic, human-readable slugs unique per server.
-- Idempotent and safe to re-run.

-- Ensure columns exist (older installations may not have these yet).
alter table if exists public.workspaces
    add column if not exists slug text,
    add column if not exists domain text;

-- Drop legacy global uniqueness (slug must be unique per server, not globally).
alter table if exists public.workspaces drop constraint if exists workspaces_slug_key;
alter table if exists public.workspaces drop constraint if exists workspaces_domain_key;

drop index if exists public.idx_workspaces_slug_unique;
drop index if exists public.idx_workspaces_domain_unique;
drop index if exists public.idx_workspaces_slug;
drop index if exists public.idx_workspaces_domain;

-- Recreate indexes with the new intended constraints.
create index if not exists idx_workspaces_server_id on public.workspaces (server_id);
create index if not exists idx_workspaces_created_at on public.workspaces (created_at desc);
create index if not exists idx_workspaces_server_slug on public.workspaces (server_id, slug);

-- Enforce slug uniqueness per server.
create unique index if not exists idx_workspaces_server_slug_unique
    on public.workspaces (server_id, slug);

-- Keep deploy domain globally unique.
create unique index if not exists idx_workspaces_domain_unique
    on public.workspaces (domain);

