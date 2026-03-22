-- ThinkSync v2 migration: Git repository integration
-- Adds workspace-linked GitHub repositories with strong access control.

create extension if not exists "uuid-ossp";

create table if not exists public.git_repos (
    id uuid primary key default uuid_generate_v4(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    provider varchar(20) not null default 'github',
    url text not null,
    branch varchar(100) default 'main',
    path text,
    is_cloned boolean not null default false,
    created_at timestamptz not null default now(),
    unique (workspace_id, url)
);

-- Ensure path column exists if table was already created without it.
alter table public.git_repos add column if not exists path text;

create index if not exists idx_git_repos_workspace_id on public.git_repos (workspace_id);
create index if not exists idx_git_repos_user_id on public.git_repos (user_id);

alter table public.git_repos enable row level security;

drop policy if exists git_repos_select_own on public.git_repos;
create policy git_repos_select_own
on public.git_repos
for select
using (auth.uid() = user_id);

drop policy if exists git_repos_insert_own on public.git_repos;
create policy git_repos_insert_own
on public.git_repos
for insert
with check (auth.uid() = user_id);

drop policy if exists git_repos_delete_own on public.git_repos;
create policy git_repos_delete_own
on public.git_repos
for delete
using (auth.uid() = user_id);
