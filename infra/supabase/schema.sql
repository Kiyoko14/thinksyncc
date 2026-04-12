-- ThinkSync v2 — Supabase schema
-- Run this in the Supabase SQL editor to set up the MVP database.

-- ── Extensions ───────────────────────────────────────────────────────────────
create extension if not exists "uuid-ossp";

-- ── servers ──────────────────────────────────────────────────────────────────
create table if not exists public.servers (
    id               uuid        primary key default uuid_generate_v4(),
    user_id          uuid        not null references auth.users(id) on delete cascade,
    name             varchar(100) not null,
    host             varchar(255) not null,
    ssh_user         varchar(100) not null,
    ssh_port         integer     not null default 22
                                 check (ssh_port between 1 and 65535),
    ssh_auth_method  varchar(20) not null
                                 check (ssh_auth_method in ('password', 'key')),
    ssh_key          text,
    ssh_password     text,
    created_at       timestamptz not null default now()
);

create table if not exists public.workspaces (
    id          uuid         primary key default uuid_generate_v4(),
    user_id     uuid         not null references auth.users(id) on delete cascade,
    server_id   uuid         not null references public.servers(id) on delete cascade,
    name        varchar(150) not null,
    path        text         not null,
    created_at  timestamptz  not null default now()
);

alter table public.servers enable row level security;
alter table public.workspaces enable row level security;

drop policy if exists "Users manage their own servers" on public.servers;
create policy "Users manage their own servers"
    on public.servers for all
    using  (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists "Users manage their own workspaces" on public.workspaces;
create policy "Users manage their own workspaces"
    on public.workspaces for all
    using  (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ── chats ────────────────────────────────────────────────────────────────────
create table if not exists public.chats (
    id          uuid        primary key default uuid_generate_v4(),
    server_id   uuid        references public.servers(id) on delete cascade,
    user_id     uuid        not null references auth.users(id) on delete cascade,
    name        varchar(255) not null,
    created_at  timestamptz not null default now()
);

alter table public.chats enable row level security;

drop policy if exists "Users manage their own chats" on public.chats;
create policy "Users manage their own chats"
    on public.chats for all
    using  (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ── messages ─────────────────────────────────────────────────────────────────
create table if not exists public.messages (
    id          uuid        primary key default uuid_generate_v4(),
    chat_id     uuid        not null references public.chats(id) on delete cascade,
    role        varchar(20) not null check (role in ('user', 'assistant', 'system')),
    content     text        not null,
    created_at  timestamptz not null default now()
);

create table if not exists public.chat_messages (
    id           uuid        primary key default uuid_generate_v4(),
    workspace_id uuid        not null references public.workspaces(id) on delete cascade,
    user_id      uuid        not null references auth.users(id) on delete cascade,
    role         varchar(20) not null check (role in ('user', 'assistant', 'system')),
    content      text        not null,
    created_at   timestamptz not null default now()
);

create table if not exists public.jobs (
    id           uuid         primary key default uuid_generate_v4(),
    user_id      uuid         not null references auth.users(id) on delete cascade,
    workspace_id uuid         references public.workspaces(id) on delete cascade,
    server_id    uuid         not null references public.servers(id) on delete cascade,
    objective    text         not null,
    status       text         not null default 'queued'
        check (status in ('queued', 'running', 'waiting_for_llm', 'completed', 'failed')),
    allow_write  boolean      not null default false,
    dry_run      boolean      not null default false,
    task_mode    text         not null default 'complex'
        check (task_mode in ('simple', 'complex')),
    plan         jsonb        not null default '[]'::jsonb,
    steps        jsonb        not null default '[]'::jsonb,
    decisions    jsonb        not null default '[]'::jsonb,
    summary      text,
    created_at   timestamptz  not null default now(),
    updated_at   timestamptz  not null default now()
);

alter table public.messages enable row level security;
alter table public.chat_messages enable row level security;
alter table public.jobs enable row level security;

drop policy if exists "Users access messages in their chats" on public.messages;
create policy "Users access messages in their chats"
    on public.messages for all
    using (
        exists (
            select 1
            from public.chats
            where chats.id = messages.chat_id
              and chats.user_id = auth.uid()
        )
    );

drop policy if exists "Users manage own workspace chat messages" on public.chat_messages;
create policy "Users manage own workspace chat messages"
    on public.chat_messages for all
    using  (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists "Users manage own jobs" on public.jobs;
create policy "Users manage own jobs"
    on public.jobs for all
    using  (auth.uid() = user_id)
    with check (auth.uid() = user_id);
