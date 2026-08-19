-- Phase 0 ROLLBACK for 20260714_google_oauth_identity.sql
-- ===========================================================================
-- Only run this if you must undo the identity migration. It reverses the
-- remap + data copy applied by 20260714_google_oauth_identity.sql.
--
-- WARNING: this DROPS public.users and the identities created in this sprint.
-- Before running, back up public.users if you need to keep the Google mappings.
-- The pre-migration state is also preserved at git tag ROLLBACK_PRE_OAUTH.
-- ===========================================================================

begin;

-- 1. Re-point FKs back to auth.users (no-op if the FK name differs).
alter table public.tasks         drop constraint if exists tasks_user_id_fkey;
alter table public.tasks         add constraint tasks_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;

alter table public.jobs          drop constraint if exists jobs_user_id_fkey;
alter table public.jobs          add constraint jobs_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;

alter table public.chat_messages drop constraint if exists chat_messages_user_id_fkey;
alter table public.chat_messages add constraint chat_messages_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;

alter table public.chats         drop constraint if exists chats_user_id_fkey;
alter table public.chats         add constraint chats_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;

alter table public.workspaces    drop constraint if exists workspaces_user_id_fkey;
alter table public.workspaces    add constraint workspaces_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;

alter table public.servers       drop constraint if exists servers_user_id_fkey;
alter table public.servers       add constraint servers_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;

-- 2. Drop the custom identity table created by the migration.
drop table if exists public.users;

commit;
