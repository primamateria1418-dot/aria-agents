-- ================================================================
-- ARIA™ — Supabase Schema
-- Run once in Supabase SQL Editor
-- OUP International Ltd, 2026
-- ================================================================

-- Enable UUID extension
create extension if not exists "uuid-ossp";

-- ── leads ──────────────────────────────────────────────────
create table if not exists leads (
  id              uuid primary key default uuid_generate_v4(),
  client_id       text not null default 'aria_internal',
  source          text,                    -- telegram / linkedin / inbound
  group_name      text,
  username        text,
  display_name    text,
  message_text    text,
  score           integer,
  score_reason    text,
  status          text default 'new',      -- new / reviewed / contacted / converted / rejected
  flagged_to      text,                    -- 'writer' if hot lead
  qualified       boolean default false,
  intent          text,
  company_signal  text,
  created_at      timestamptz default now()
);

create index if not exists leads_client_id_idx on leads(client_id);
create index if not exists leads_score_idx on leads(score);
create index if not exists leads_status_idx on leads(status);

-- ── content_queue ──────────────────────────────────────────
create table if not exists content_queue (
  id              uuid primary key default uuid_generate_v4(),
  client_id       text not null default 'aria_internal',
  content_type    text,                    -- linkedin_post / email / blog / discord_reply
  platform        text,
  draft           text,
  approved        boolean default false,
  approved_at     timestamptz,
  published       boolean default false,
  published_at    timestamptz,
  lead_id         uuid references leads(id) on delete set null,
  created_by      text,                    -- 'research' / 'writer' / 'reporting'
  created_at      timestamptz default now()
);

create index if not exists content_queue_client_id_idx on content_queue(client_id);
create index if not exists content_queue_approved_idx on content_queue(approved);
create index if not exists content_queue_published_idx on content_queue(published);

-- ── instructions ───────────────────────────────────────────
create table if not exists instructions (
  id              uuid primary key default uuid_generate_v4(),
  client_id       text not null default 'aria_internal',
  target_agent    text,                    -- 'jamie' / 'writer' / 'all'
  instruction     text,
  issued_by       text default 'ceo',
  active          boolean default true,
  expires_at      timestamptz,
  created_at      timestamptz default now()
);

create index if not exists instructions_client_agent_idx on instructions(client_id, target_agent);
create index if not exists instructions_active_idx on instructions(active);

-- ── reports ────────────────────────────────────────────────
create table if not exists reports (
  id              uuid primary key default uuid_generate_v4(),
  client_id       text not null default 'aria_internal',
  agent_name      text,
  cycle_date      date,
  summary         text,
  metrics         jsonb default '{}',
  flags_raised    integer default 0,
  flags_received  integer default 0,
  created_at      timestamptz default now()
);

create index if not exists reports_client_agent_idx on reports(client_id, agent_name);
create index if not exists reports_date_idx on reports(cycle_date);

-- ── agent_memory ───────────────────────────────────────────
create table if not exists agent_memory (
  id                  uuid primary key default uuid_generate_v4(),
  client_id           text not null default 'aria_internal',
  agent_name          text,
  cycle_date          date,
  actions_taken       text,
  outcomes            text,
  performance_rating  integer,
  what_worked         text,
  what_to_change      text,
  peer_observations   text,
  next_cycle_intent   text,
  raw_reflection      jsonb default '{}',
  created_at          timestamptz default now()
);

create index if not exists agent_memory_client_agent_idx on agent_memory(client_id, agent_name);
create index if not exists agent_memory_date_idx on agent_memory(cycle_date);

-- ── agent_flags ────────────────────────────────────────────
create table if not exists agent_flags (
  id              uuid primary key default uuid_generate_v4(),
  client_id       text not null default 'aria_internal',
  from_agent      text,
  to_agent        text,
  priority        text default 'normal',  -- urgent / normal / low
  message         text,
  context         jsonb default '{}',
  resolved        boolean default false,
  resolved_at     timestamptz,
  resolved_by     text,
  created_at      timestamptz default now()
);

create index if not exists agent_flags_client_to_idx on agent_flags(client_id, to_agent);
create index if not exists agent_flags_resolved_idx on agent_flags(resolved);

-- ── logs ───────────────────────────────────────────────────
create table if not exists logs (
  id              uuid primary key default uuid_generate_v4(),
  client_id       text not null default 'aria_internal',
  agent_name      text,
  event_type      text,                   -- cycle_start / cycle_end / error / flag_raised
  message         text,
  metadata        jsonb default '{}',
  created_at      timestamptz default now()
);

create index if not exists logs_client_agent_idx on logs(client_id, agent_name);
create index if not exists logs_event_type_idx on logs(event_type);

-- ================================================================
-- Verify
-- ================================================================
select table_name from information_schema.tables
where table_schema = 'public'
order by table_name;
