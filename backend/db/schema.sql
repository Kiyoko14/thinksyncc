-- ThinkSync production schema — SINGLE SOURCE OF TRUTH
-- This file is the exact final database state, equivalent to applying
-- db/schema.sql (legacy base) followed by every migration in db/migrations/
-- in filename order. Regenerate via: pg_dump --schema-only --no-owner.
-- A fresh PostgreSQL instance created ONLY from this file behaves identically
-- to one built from all migrations.

create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";

--
--

\restrict gJvBz6fXnv7gS9NxHnc8et36QNPXJwopShD3kyEqFSP2Hb5J46xue5gdlZhvutO

-- Dumped from database version 14.23 (Ubuntu 14.23-0ubuntu0.22.04.1)

--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';

--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;

--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';

--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
begin
    new.updated_at = now();
    return new;
end;
$$;

--
-- Name: agent_context_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_context_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workspace_id uuid NOT NULL,
    task text NOT NULL,
    selected_files jsonb DEFAULT '[]'::jsonb NOT NULL,
    snippet_preview text,
    source text DEFAULT 'fresh'::text NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: agent_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    user_email text NOT NULL,
    server_id uuid NOT NULL,
    objective text NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    allow_write boolean DEFAULT false NOT NULL,
    max_steps integer NOT NULL,
    plan jsonb NOT NULL,
    results jsonb NOT NULL,
    summary text NOT NULL,
    success boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: approval_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approval_audit (
    event_id text NOT NULL,
    approval_id text NOT NULL,
    job_id uuid NOT NULL,
    conversation_id text NOT NULL,
    event_type text NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now(),
    decision text,
    reason text DEFAULT ''::text,
    "user" text DEFAULT ''::text,
    metadata jsonb DEFAULT '{}'::jsonb
);

--
-- Name: approval_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approval_requests (
    approval_id text NOT NULL,
    job_id uuid NOT NULL,
    conversation_id text NOT NULL,
    approval_type text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    title text DEFAULT ''::text,
    description text DEFAULT ''::text,
    risk_level text DEFAULT 'medium'::text,
    affected_files jsonb DEFAULT '[]'::jsonb,
    affected_commands jsonb DEFAULT '[]'::jsonb,
    affected_assumptions jsonb DEFAULT '[]'::jsonb,
    context jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    resolved_at timestamp with time zone,
    resolved_by text DEFAULT ''::text,
    decision text,
    reason text DEFAULT ''::text,
    spec_version integer,
    requirement_version integer,
    request_version integer DEFAULT 0 NOT NULL,
    resume_token jsonb
);

--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workspace_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chat_messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text, 'system'::text])))
);

--
-- Name: chats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chats (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    server_id uuid,
    user_id uuid NOT NULL,
    name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    workspace_id uuid
);

--
-- Name: conversation_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_audit (
    event_id text NOT NULL,
    job_id text NOT NULL,
    conversation_id text DEFAULT ''::text NOT NULL,
    session_id text DEFAULT ''::text NOT NULL,
    event_type text NOT NULL,
    actor text DEFAULT 'user'::text NOT NULL,
    content jsonb DEFAULT '{}'::jsonb NOT NULL,
    spec_version integer,
    cursor_version integer,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: idempotency_store; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.idempotency_store (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    operation_id text NOT NULL,
    job_id text NOT NULL,
    result jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: job_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_decisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    step_number integer,
    action text NOT NULL,
    reason text,
    summary_so_far text,
    modified_step jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: job_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    workspace_id uuid,
    sequence bigint DEFAULT 0 NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    trace_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: job_execution_details; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_execution_details (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    detail_type text NOT NULL,
    step_number integer,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_execution_details_detail_type_check CHECK ((detail_type = ANY (ARRAY['error'::text, 'metadata'::text, 'analysis'::text, 'contract'::text])))
);

--
-- Name: job_retries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_retries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    step_number integer NOT NULL,
    attempt integer NOT NULL,
    command text,
    command_type text,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: job_state_transitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_state_transitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    from_status text,
    to_status text NOT NULL,
    step integer,
    tool text,
    trace_id text,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_state_transitions_from_status_check CHECK ((from_status = ANY (ARRAY['queued'::text, 'running'::text, 'waiting_for_llm'::text, 'completed'::text, 'failed'::text]))),
    CONSTRAINT job_state_transitions_to_status_check CHECK ((to_status = ANY (ARRAY['queued'::text, 'running'::text, 'waiting_for_llm'::text, 'completed'::text, 'failed'::text])))
);

--
-- Name: job_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_steps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    step_number integer NOT NULL,
    tool text NOT NULL,
    args jsonb DEFAULT '{}'::jsonb NOT NULL,
    command text,
    command_type text,
    stdout text,
    stderr text,
    exit_code integer,
    duration_ms integer,
    success boolean DEFAULT false NOT NULL,
    validation_passed boolean DEFAULT false NOT NULL,
    status text,
    agent_reasoning text,
    executed_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    workspace_id uuid,
    server_id uuid NOT NULL,
    objective text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    allow_write boolean DEFAULT false NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    task_mode text DEFAULT 'complex'::text NOT NULL,
    plan jsonb DEFAULT '[]'::jsonb NOT NULL,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    decisions jsonb DEFAULT '[]'::jsonb NOT NULL,
    errors jsonb DEFAULT '[]'::jsonb NOT NULL,
    retries jsonb DEFAULT '[]'::jsonb NOT NULL,
    summary text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    recoverable boolean DEFAULT false NOT NULL,
    recovery_reason text,
    intent text DEFAULT 'chat'::text NOT NULL,
    worker_id text,
    claimed_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    completed_at timestamp with time zone,
    conversation_id text,
    conversation_session jsonb,
    cursor_version integer DEFAULT 0 NOT NULL,
    spec jsonb,
    CONSTRAINT jobs_intent_check CHECK ((intent = ANY (ARRAY['chat'::text, 'code'::text, 'server'::text]))),
    CONSTRAINT jobs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'claimed'::text, 'running'::text, 'waiting_for_llm'::text, 'waiting_for_user'::text, 'approved'::text, 'rejected'::text, 'resumed'::text, 'paused'::text, 'cancelled'::text, 'completed'::text, 'failed'::text, 'abandoned'::text, 'recoverable'::text]))),
    CONSTRAINT jobs_task_mode_check CHECK ((task_mode = ANY (ARRAY['simple'::text, 'complex'::text])))
);

--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    chat_id uuid NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text, 'system'::text])))
);

--
-- Name: project_specifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_specifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    conversation_id text NOT NULL,
    user_id text DEFAULT ''::text NOT NULL,
    spec_json jsonb NOT NULL,
    spec_versions jsonb DEFAULT '[]'::jsonb NOT NULL,
    latest_spec_version integer DEFAULT 0 NOT NULL,
    approval_context jsonb DEFAULT '{}'::jsonb NOT NULL,
    requirement_events jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: resume_outcomes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resume_outcomes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    approval_id text NOT NULL,
    job_id uuid NOT NULL,
    resume_result jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.servers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    name text NOT NULL,
    host text NOT NULL,
    ssh_user text NOT NULL,
    ssh_port integer DEFAULT 22 NOT NULL,
    ssh_auth_method text NOT NULL,
    ssh_key text,
    ssh_password text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT servers_ssh_auth_method_check CHECK ((ssh_auth_method = ANY (ARRAY['private_key'::text, 'password'::text]))),
    CONSTRAINT servers_ssh_port_check CHECK (((ssh_port >= 1) AND (ssh_port <= 65535)))
);

--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    chat_id uuid NOT NULL,
    user_id uuid NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email text,
    google_sub text,
    display_name text,
    avatar_url text,
    provider text DEFAULT 'google'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: worker_heartbeats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.worker_heartbeats (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    worker_id text NOT NULL,
    job_id uuid,
    last_heartbeat timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT worker_heartbeats_status_check CHECK ((status = ANY (ARRAY['active'::text, 'idle'::text, 'stale'::text, 'shutdown'::text])))
);

--
-- Name: workspace_deployments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_deployments (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    workspace_id uuid NOT NULL,
    port integer NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workspace_deployments_port_check CHECK (((port >= 1024) AND (port <= 65535)))
);

--
-- Name: workspace_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_files (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workspace_id uuid NOT NULL,
    path text NOT NULL,
    size bigint DEFAULT 0 NOT NULL,
    last_modified timestamp with time zone,
    language text DEFAULT 'unknown'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: workspaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspaces (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    server_id uuid NOT NULL,
    name text NOT NULL,
    -- Original user-provided workspace name (human-readable). Kept separate
    -- from the sanitized `name`/`slug`. Falls back to `name` for legacy rows.
    display_name text,
    path text NOT NULL,
    slug text NOT NULL,
    domain text NOT NULL,
    github_connection_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

-- Name: github_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.github_connections (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    name text NOT NULL,
    auth_method text NOT NULL DEFAULT 'ssh',
    host text NOT NULL DEFAULT 'github.com',
    ssh_public_key text,
    ssh_private_key text,
    ssh_key_type text,
    installation_id text,
    repo_id bigint,
    repo_full_name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: agent_context_logs agent_context_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_context_logs
    ADD CONSTRAINT agent_context_logs_pkey PRIMARY KEY (id);

--
-- Name: agent_runs agent_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (id);

--
-- Name: approval_audit approval_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_audit
    ADD CONSTRAINT approval_audit_pkey PRIMARY KEY (event_id);

--
-- Name: approval_requests approval_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_requests
    ADD CONSTRAINT approval_requests_pkey PRIMARY KEY (approval_id);

--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);

--
-- Name: chats chats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chats
    ADD CONSTRAINT chats_pkey PRIMARY KEY (id);

--
-- Name: conversation_audit conversation_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_audit
    ADD CONSTRAINT conversation_audit_pkey PRIMARY KEY (event_id);

--
-- Name: idempotency_store idempotency_store_operation_id_job_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_store
    ADD CONSTRAINT idempotency_store_operation_id_job_id_key UNIQUE (operation_id, job_id);

--
-- Name: idempotency_store idempotency_store_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idempotency_store
    ADD CONSTRAINT idempotency_store_pkey PRIMARY KEY (id);

--
-- Name: job_decisions job_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_decisions
    ADD CONSTRAINT job_decisions_pkey PRIMARY KEY (id);

--
-- Name: job_events job_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_events
    ADD CONSTRAINT job_events_pkey PRIMARY KEY (id);

--
-- Name: job_execution_details job_execution_details_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_execution_details
    ADD CONSTRAINT job_execution_details_pkey PRIMARY KEY (id);

--
-- Name: job_retries job_retries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_retries
    ADD CONSTRAINT job_retries_pkey PRIMARY KEY (id);

--
-- Name: job_state_transitions job_state_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_state_transitions
    ADD CONSTRAINT job_state_transitions_pkey PRIMARY KEY (id);

--
-- Name: job_steps job_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_steps
    ADD CONSTRAINT job_steps_pkey PRIMARY KEY (id);

--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);

--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);

--
-- Name: project_specifications project_specifications_conversation_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_specifications
    ADD CONSTRAINT project_specifications_conversation_id_key UNIQUE (conversation_id);

--
-- Name: project_specifications project_specifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_specifications
    ADD CONSTRAINT project_specifications_pkey PRIMARY KEY (id);

--
-- Name: resume_outcomes resume_outcomes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resume_outcomes
    ADD CONSTRAINT resume_outcomes_pkey PRIMARY KEY (id);

--
-- Name: servers servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.servers
    ADD CONSTRAINT servers_pkey PRIMARY KEY (id);

--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);

--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

--
-- Name: worker_heartbeats worker_heartbeats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_heartbeats
    ADD CONSTRAINT worker_heartbeats_pkey PRIMARY KEY (id);

--
-- Name: worker_heartbeats worker_heartbeats_worker_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_heartbeats
    ADD CONSTRAINT worker_heartbeats_worker_id_key UNIQUE (worker_id);

--
-- Name: workspace_deployments workspace_deployments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_deployments
    ADD CONSTRAINT workspace_deployments_pkey PRIMARY KEY (id);

--
-- Name: workspace_files workspace_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_files
    ADD CONSTRAINT workspace_files_pkey PRIMARY KEY (id);

--
-- Name: workspaces workspaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_pkey PRIMARY KEY (id);

--
-- Name: idx_agent_context_logs_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_context_logs_workspace_id ON public.agent_context_logs USING btree (workspace_id, "timestamp" DESC);

--
-- Name: idx_agent_runs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_runs_created_at ON public.agent_runs USING btree (created_at DESC);

--
-- Name: idx_agent_runs_server_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_runs_server_id ON public.agent_runs USING btree (server_id);

--
-- Name: idx_agent_runs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_runs_user_id ON public.agent_runs USING btree (user_id);

--
-- Name: idx_approval_audit_approval; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approval_audit_approval ON public.approval_audit USING btree (approval_id);

--
-- Name: idx_approval_audit_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approval_audit_job ON public.approval_audit USING btree (job_id);

--
-- Name: idx_approval_requests_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approval_requests_job ON public.approval_requests USING btree (job_id);

--
-- Name: idx_approval_requests_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approval_requests_status ON public.approval_requests USING btree (status);

--
-- Name: idx_chat_messages_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_messages_user_id ON public.chat_messages USING btree (user_id, created_at);

--
-- Name: idx_chat_messages_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_messages_workspace_id ON public.chat_messages USING btree (workspace_id, created_at);

--
-- Name: idx_chats_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chats_created_at ON public.chats USING btree (created_at DESC);

--
-- Name: idx_chats_server_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chats_server_id ON public.chats USING btree (server_id);

--
-- Name: idx_chats_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chats_user_id ON public.chats USING btree (user_id);

--
-- Name: idx_chats_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chats_workspace_id ON public.chats USING btree (workspace_id);

--
-- Name: idx_conversation_audit_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_audit_conversation ON public.conversation_audit USING btree (conversation_id);

--
-- Name: idx_conversation_audit_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_audit_job ON public.conversation_audit USING btree (job_id);

--
-- Name: idx_conversation_audit_job_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_audit_job_event ON public.conversation_audit USING btree (job_id, event_type);

--
-- Name: idx_idempotency_store_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_idempotency_store_job ON public.idempotency_store USING btree (job_id);

--
-- Name: idx_idempotency_store_op_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_idempotency_store_op_job ON public.idempotency_store USING btree (operation_id, job_id);

--
-- Name: idx_job_decisions_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_decisions_job_id ON public.job_decisions USING btree (job_id, created_at DESC);

--
-- Name: idx_job_events_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_events_job_id ON public.job_events USING btree (job_id, created_at DESC);

--
-- Name: idx_job_events_job_sequence; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_job_events_job_sequence ON public.job_events USING btree (job_id, sequence);

--
-- Name: idx_job_events_trace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_events_trace_id ON public.job_events USING btree (trace_id);

--
-- Name: idx_job_events_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_events_workspace_id ON public.job_events USING btree (workspace_id, created_at DESC);

--
-- Name: idx_job_execution_details_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_execution_details_job_id ON public.job_execution_details USING btree (job_id, detail_type, created_at DESC);

--
-- Name: idx_job_retries_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_retries_job_id ON public.job_retries USING btree (job_id, created_at DESC);

--
-- Name: idx_job_retries_job_step; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_retries_job_step ON public.job_retries USING btree (job_id, step_number);

--
-- Name: idx_job_state_transitions_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_state_transitions_job_id ON public.job_state_transitions USING btree (job_id, created_at DESC);

--
-- Name: idx_job_steps_executed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_steps_executed_at ON public.job_steps USING btree (job_id, executed_at DESC);

--
-- Name: idx_job_steps_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_steps_job_id ON public.job_steps USING btree (job_id, step_number);

--
-- Name: idx_jobs_conversation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_conversation_id ON public.jobs USING btree (conversation_id);

--
-- Name: idx_jobs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_created_at ON public.jobs USING btree (created_at DESC);

--
-- Name: idx_jobs_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_deleted_at ON public.jobs USING btree (deleted_at) WHERE (deleted_at IS NULL);

--
-- Name: idx_jobs_intent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_intent ON public.jobs USING btree (intent, created_at DESC);

--
-- Name: idx_jobs_recoverable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_recoverable ON public.jobs USING btree (recoverable, status) WHERE (deleted_at IS NULL);

--
-- Name: idx_jobs_server_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_server_id ON public.jobs USING btree (server_id);

--
-- Name: idx_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_status ON public.jobs USING btree (status);

--
-- Name: idx_jobs_status_claimed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_status_claimed ON public.jobs USING btree (status, claimed_at) WHERE ((deleted_at IS NULL) AND (status = 'queued'::text));

--
-- Name: idx_jobs_status_heartbeat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_status_heartbeat ON public.jobs USING btree (status, heartbeat_at) WHERE ((deleted_at IS NULL) AND (status = ANY (ARRAY['claimed'::text, 'running'::text, 'waiting_for_llm'::text])));

--
-- Name: idx_jobs_status_worker_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_status_worker_id ON public.jobs USING btree (status, worker_id) WHERE (deleted_at IS NULL);

--
-- Name: idx_jobs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_user_id ON public.jobs USING btree (user_id);

--
-- Name: idx_jobs_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_workspace_id ON public.jobs USING btree (workspace_id, created_at DESC);

--
-- Name: idx_messages_chat_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_chat_id ON public.messages USING btree (chat_id);

--
-- Name: idx_messages_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_created_at ON public.messages USING btree (created_at);

--
-- Name: idx_project_specs_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_specs_conversation ON public.project_specifications USING btree (conversation_id);

--
-- Name: idx_resume_outcomes_approval; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resume_outcomes_approval ON public.resume_outcomes USING btree (approval_id);

--
-- Name: idx_resume_outcomes_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resume_outcomes_job ON public.resume_outcomes USING btree (job_id);

--
-- Name: idx_servers_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_servers_user_id ON public.servers USING btree (user_id);

--
-- Name: idx_tasks_chat_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_chat_id ON public.tasks USING btree (chat_id);

--
-- Name: idx_tasks_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_user_id ON public.tasks USING btree (user_id);

--
-- Name: idx_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_users_email ON public.users USING btree (email) WHERE (email IS NOT NULL);

--
-- Name: idx_users_google_sub; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_users_google_sub ON public.users USING btree (google_sub) WHERE (google_sub IS NOT NULL);

--
-- Name: idx_worker_heartbeats_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_worker_heartbeats_job ON public.worker_heartbeats USING btree (job_id);

--
-- Name: idx_workspace_deployments_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspace_deployments_is_active ON public.workspace_deployments USING btree (is_active);

--
-- Name: idx_workspace_deployments_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspace_deployments_workspace_id ON public.workspace_deployments USING btree (workspace_id);

--
-- Name: idx_workspace_files_workspace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspace_files_workspace_id ON public.workspace_files USING btree (workspace_id, updated_at DESC);

--
-- Name: idx_workspace_files_workspace_path_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_workspace_files_workspace_path_unique ON public.workspace_files USING btree (workspace_id, path);

--
-- Name: idx_workspaces_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspaces_created_at ON public.workspaces USING btree (created_at DESC);

--
-- Name: idx_workspaces_domain_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_workspaces_domain_unique ON public.workspaces USING btree (domain);

--
-- Name: idx_workspaces_server_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspaces_server_id ON public.workspaces USING btree (server_id);

--
-- Name: idx_workspaces_server_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspaces_server_slug ON public.workspaces USING btree (server_id, slug);

--
-- Name: idx_workspaces_server_slug_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_workspaces_server_slug_unique ON public.workspaces USING btree (server_id, slug);

--
-- Name: idx_workspaces_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspaces_user_id ON public.workspaces USING btree (user_id);

--
-- Name: idx_workspaces_user_server_name_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_workspaces_user_server_name_unique ON public.workspaces USING btree (user_id, server_id, lower(name));

--
-- Name: chats trg_chats_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_chats_set_updated_at BEFORE UPDATE ON public.chats FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: jobs trg_jobs_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jobs_set_updated_at BEFORE UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: servers trg_servers_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_servers_set_updated_at BEFORE UPDATE ON public.servers FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: workspace_files trg_workspace_files_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_workspace_files_set_updated_at BEFORE UPDATE ON public.workspace_files FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: workspaces trg_workspaces_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_workspaces_set_updated_at BEFORE UPDATE ON public.workspaces FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

--
-- Name: agent_context_logs agent_context_logs_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_context_logs
    ADD CONSTRAINT agent_context_logs_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;

--
-- Name: approval_audit approval_audit_approval_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_audit
    ADD CONSTRAINT approval_audit_approval_id_fkey FOREIGN KEY (approval_id) REFERENCES public.approval_requests(approval_id) ON DELETE CASCADE;

--
-- Name: approval_audit approval_audit_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_audit
    ADD CONSTRAINT approval_audit_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: approval_requests approval_requests_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approval_requests
    ADD CONSTRAINT approval_requests_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: chat_messages chat_messages_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: chat_messages chat_messages_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;

--
-- Name: chats chats_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chats
    ADD CONSTRAINT chats_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.servers(id) ON DELETE CASCADE;

--
-- Name: chats chats_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chats
    ADD CONSTRAINT chats_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: chats chats_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chats
    ADD CONSTRAINT chats_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;

--
-- Name: job_decisions job_decisions_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_decisions
    ADD CONSTRAINT job_decisions_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: job_events job_events_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_events
    ADD CONSTRAINT job_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: job_events job_events_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_events
    ADD CONSTRAINT job_events_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE SET NULL;

--
-- Name: job_execution_details job_execution_details_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_execution_details
    ADD CONSTRAINT job_execution_details_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: job_retries job_retries_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_retries
    ADD CONSTRAINT job_retries_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: job_state_transitions job_state_transitions_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_state_transitions
    ADD CONSTRAINT job_state_transitions_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: job_steps job_steps_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_steps
    ADD CONSTRAINT job_steps_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: jobs jobs_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.servers(id) ON DELETE CASCADE;

--
-- Name: jobs jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: jobs jobs_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;

--
-- Name: messages messages_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;

--
-- Name: resume_outcomes resume_outcomes_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resume_outcomes
    ADD CONSTRAINT resume_outcomes_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;

--
-- Name: servers servers_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.servers
    ADD CONSTRAINT servers_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: tasks tasks_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;

--
-- Name: tasks tasks_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: worker_heartbeats worker_heartbeats_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_heartbeats
    ADD CONSTRAINT worker_heartbeats_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;

--
-- Name: workspace_deployments workspace_deployments_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_deployments
    ADD CONSTRAINT workspace_deployments_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;

--
-- Name: workspace_files workspace_files_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_files
    ADD CONSTRAINT workspace_files_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;

--
-- Name: workspaces workspaces_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_server_id_fkey FOREIGN KEY (server_id) REFERENCES public.servers(id) ON DELETE CASCADE;

--
-- Name: workspaces workspaces_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: agent_context_logs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_context_logs ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_runs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_runs ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_audit; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.approval_audit ENABLE ROW LEVEL SECURITY;

--
-- Name: approval_requests; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.approval_requests ENABLE ROW LEVEL SECURITY;

--
-- Name: chat_messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;

--
-- Name: chats; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chats ENABLE ROW LEVEL SECURITY;

--
-- Name: conversation_audit; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.conversation_audit ENABLE ROW LEVEL SECURITY;

--
-- Name: idempotency_store; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.idempotency_store ENABLE ROW LEVEL SECURITY;

--
-- Name: job_decisions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.job_decisions ENABLE ROW LEVEL SECURITY;

--
-- Name: job_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.job_events ENABLE ROW LEVEL SECURITY;

--
-- Name: job_execution_details; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.job_execution_details ENABLE ROW LEVEL SECURITY;

--
-- Name: job_retries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.job_retries ENABLE ROW LEVEL SECURITY;

--
-- Name: job_state_transitions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.job_state_transitions ENABLE ROW LEVEL SECURITY;

--
-- Name: job_steps; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.job_steps ENABLE ROW LEVEL SECURITY;

--
-- Name: jobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;

--
-- Name: messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;

--
-- Name: project_specifications; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.project_specifications ENABLE ROW LEVEL SECURITY;

--
-- Name: resume_outcomes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.resume_outcomes ENABLE ROW LEVEL SECURITY;

--
-- Name: worker_heartbeats; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.worker_heartbeats ENABLE ROW LEVEL SECURITY;

--
-- Name: workspace_deployments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspace_deployments ENABLE ROW LEVEL SECURITY;

--
-- Name: workspace_files; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspace_files ENABLE ROW LEVEL SECURITY;

--
-- Name: workspaces; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workspaces ENABLE ROW LEVEL SECURITY;

--
--

\unrestrict gJvBz6fXnv7gS9NxHnc8et36QNPXJwopShD3kyEqFSP2Hb5J46xue5gdlZhvutO
