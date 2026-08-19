-- ThinkSync v2 migration: workspace-scoped chat system
-- This migration is additive/backward-safe and idempotent.

create extension if not exists "uuid-ossp";

-- Workspaces represent filesystem directories on a user's server.
create table if not exists public.workspaces (
    id uuid primary key default uuid_generate_v4(),
    user_id uuid not null references auth.users(id) on delete cascade,
    server_id uuid not null references public.servers(id) on delete cascade,
    name varchar(150) not null,
    path text not null,
    created_at timestamptz not null default now()
);

-- Case-insensitive unique workspace name per user/server pair.
create unique index if not exists idx_workspaces_user_server_name_unique
    on public.workspaces (user_id, server_id, lower(name));
create index if not exists idx_workspaces_user_id on public.workspaces (user_id);
create index if not exists idx_workspaces_server_id on public.workspaces (server_id);
create index if not exists idx_workspaces_created_at on public.workspaces (created_at desc);

-- Existing chats table may already exist with a server-scoped shape.
-- Keep old columns for compatibility and extend for workspace-scoped chat.
create table if not exists public.chats (
    id uuid primary key default uuid_generate_v4(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    created_at timestamptz not null default now()
);

alter table public.chats add column if not exists workspace_id uuid;
alter table public.chats add column if not exists user_id uuid;
alter table public.chats add column if not exists created_at timestamptz not null default now();

-- Allow new workspace-only chat writes even if legacy columns are present.
alter table public.chats alter column server_id drop not null;
alter table public.chats alter column name drop not null;

-- Ensure the expected FK exists for workspace_id.
do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'chats_workspace_id_fkey'
          and conrelid = 'public.chats'::regclass
    ) then
        alter table public.chats
            add constraint chats_workspace_id_fkey
            foreign key (workspace_id)
            references public.workspaces(id)
            on delete cascade;
    end if;
end $$;

create index if not exists idx_chats_workspace_id on public.chats (workspace_id);
create index if not exists idx_chats_user_id on public.chats (user_id);
create index if not exists idx_chats_created_at on public.chats (created_at desc);

-- Existing messages table may already exist; ensure expected columns/checks are present.
create table if not exists public.messages (
    id uuid primary key default uuid_generate_v4(),
    chat_id uuid not null references public.chats(id) on delete cascade,
    role varchar(20) not null,
    content text not null,
    created_at timestamptz not null default now()
);

alter table public.messages add column if not exists chat_id uuid;
alter table public.messages add column if not exists role varchar(20);
alter table public.messages add column if not exists content text;
alter table public.messages add column if not exists created_at timestamptz not null default now();

-- Ensure role validation constraint exists.
do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'messages_role_check'
          and conrelid = 'public.messages'::regclass
    ) then
        alter table public.messages
            add constraint messages_role_check
            check (role in ('user', 'assistant', 'system'));
    end if;
end $$;

create index if not exists idx_messages_chat_id on public.messages (chat_id);
create index if not exists idx_messages_created_at on public.messages (created_at);

-- RLS
alter table public.workspaces enable row level security;
alter table public.chats enable row level security;
alter table public.messages enable row level security;

drop policy if exists "Users manage own workspaces" on public.workspaces;
create policy "Users manage own workspaces"
on public.workspaces
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users manage own chats" on public.chats;
create policy "Users manage own chats"
on public.chats
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users access messages in own chats" on public.messages;
create policy "Users access messages in own chats"
on public.messages
for all
using (
    exists (
        select 1
        from public.chats c
        where c.id = messages.chat_id
          and c.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from public.chats c
        where c.id = messages.chat_id
          and c.user_id = auth.uid()
    )
);
