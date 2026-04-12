-- Workspace-scoped persistent chat memory for the remote execution agent.

create extension if not exists "pgcrypto";

create table if not exists public.chat_messages (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    role varchar(20) not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_chat_messages_workspace_id
    on public.chat_messages (workspace_id, created_at);
create index if not exists idx_chat_messages_user_id
    on public.chat_messages (user_id, created_at);

alter table public.chat_messages enable row level security;

drop policy if exists "Users manage own workspace chat messages" on public.chat_messages;
create policy "Users manage own workspace chat messages"
on public.chat_messages
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);
