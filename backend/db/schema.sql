-- ThinkSync MVP schema
-- Single source of truth: backend/db/schema.sql

create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";

create table if not exists public.servers (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    host text not null,
    ssh_user text not null,
    ssh_port integer not null default 22 check (ssh_port between 1 and 65535),
    ssh_auth_method text not null check (ssh_auth_method in ('private_key', 'password')),
    ssh_key text,
    ssh_password text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.chats (
    id uuid primary key default gen_random_uuid(),
    server_id uuid not null references public.servers(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.messages (
    id uuid primary key default gen_random_uuid(),
    chat_id uuid not null references public.chats(id) on delete cascade,
    role text not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    created_at timestamptz not null default now()
);

create table if not exists public.tasks (
    id uuid primary key default gen_random_uuid(),
    chat_id uuid not null references public.chats(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    state text not null default 'pending',
    created_at timestamptz not null default now()
);

create index if not exists idx_servers_user_id on public.servers (user_id);
create index if not exists idx_chats_user_id on public.chats (user_id);
create index if not exists idx_chats_server_id on public.chats (server_id);
create index if not exists idx_messages_chat_id on public.messages (chat_id);
create index if not exists idx_tasks_user_id on public.tasks (user_id);
create index if not exists idx_tasks_chat_id on public.tasks (chat_id);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_servers_set_updated_at on public.servers;
create trigger trg_servers_set_updated_at
before update on public.servers
for each row
execute function public.set_updated_at();

drop trigger if exists trg_chats_set_updated_at on public.chats;
create trigger trg_chats_set_updated_at
before update on public.chats
for each row
execute function public.set_updated_at();

alter table public.servers enable row level security;
alter table public.chats enable row level security;
alter table public.messages enable row level security;
alter table public.tasks enable row level security;

drop policy if exists "Users manage own servers" on public.servers;
create policy "Users manage own servers"
on public.servers
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users manage own chats" on public.chats;
create policy "Users manage own chats"
on public.chats
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users manage own messages" on public.messages;
create policy "Users manage own messages"
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

drop policy if exists "Users manage own tasks" on public.tasks;
create policy "Users manage own tasks"
on public.tasks
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);
