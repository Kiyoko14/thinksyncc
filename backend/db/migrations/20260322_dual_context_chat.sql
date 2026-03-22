-- ThinkSync v2 migration: Dual-context chat system (workspace + git repo)
-- Enables chat to belong to either a workspace or a git repository, but not both.

-- First, ensure git_repos table has a path column if it doesn't already.
alter table if exists public.git_repos add column if not exists path text;

-- Modify chats table to support both workspace and git repo contexts.
alter table public.chats add column if not exists workspace_id uuid references public.workspaces(id) on delete cascade;
alter table public.chats add column if not exists git_repo_id uuid references public.git_repos(id) on delete cascade;

-- Add constraint: exactly one of workspace_id or git_repo_id must be set.
alter table public.chats drop constraint if exists chats_context_check;
alter table public.chats add constraint chats_context_check
check (
    (workspace_id is not null and git_repo_id is null)
    or
    (workspace_id is null and git_repo_id is not null)
);

-- Create indexes for efficient lookups.
create index if not exists idx_chats_workspace_id on public.chats (workspace_id) where workspace_id is not null;
create index if not exists idx_chats_git_repo_id on public.chats (git_repo_id) where git_repo_id is not null;

-- Ensure RLS remains enabled.
alter table public.chats enable row level security;

-- Update RLS policy to account for git_repo context as well.
drop policy if exists "Users manage own chats" on public.chats;
create policy "Users manage own chats"
on public.chats
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);
