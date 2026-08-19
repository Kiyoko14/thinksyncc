-- Static deployments are served by Nginx and have no application port.
alter table public.workspace_deployments
    alter column port drop not null;

alter table public.workspace_deployments
    add column if not exists runtime text not null default 'python';

alter table public.workspace_deployments
    add column if not exists verified boolean not null default false;

alter table public.workspace_deployments
    add constraint workspace_deployments_runtime_check
    check (runtime in ('static', 'python', 'node'));