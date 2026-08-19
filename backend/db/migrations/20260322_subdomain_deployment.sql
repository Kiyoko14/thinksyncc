-- ThinkSync v2 migration: Workspace subdomain deployment system
-- Adds unique slug and domain to enable per-workspace subdomains.

-- Add columns to workspaces table.
alter table if exists public.workspaces add column if not exists slug text unique not null default '';
alter table if exists public.workspaces add column if not exists domain text unique not null default '';

-- Create indexes for efficient lookups by slug and domain.
create index if not exists idx_workspaces_slug on public.workspaces (slug);
create index if not exists idx_workspaces_domain on public.workspaces (domain);

-- Ensure uniqueness is properly enforced.
create unique index if not exists idx_workspaces_slug_unique on public.workspaces (slug);
create unique index if not exists idx_workspaces_domain_unique on public.workspaces (domain);

-- Optional: Add deployment table for domain → port mapping.
create table if not exists public.workspace_deployments (
    id uuid primary key default uuid_generate_v4(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    port integer not null check (port between 1024 and 65535),
    is_active boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_workspace_deployments_workspace_id on public.workspace_deployments (workspace_id);
create index if not exists idx_workspace_deployments_is_active on public.workspace_deployments (is_active);

alter table public.workspace_deployments enable row level security;

drop policy if exists "Users manage own deployments" on public.workspace_deployments;
create policy "Users manage own deployments"
on public.workspace_deployments
for all
using (
    exists (
        select 1
        from public.workspaces w
        where w.id = workspace_deployments.workspace_id
          and w.user_id = auth.uid()
    )
);
