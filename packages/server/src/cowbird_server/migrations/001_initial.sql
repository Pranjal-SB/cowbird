create table if not exists addresses (
  value      text primary key,
  provider   text not null,
  state      text,
  expires_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists addresses_expires_at on addresses (expires_at);

-- Keyed by instance: each row has exactly one writer, so two instances can
-- never contend for it and no locking is needed on this path.
create table if not exists provider_health (
  instance_id          text not null,
  provider             text not null,
  status               text not null,
  latencies            double precision[] not null default '{}',
  last_checked         timestamptz,
  last_failure         text,
  needs_residential_ip boolean not null default false,
  primary key (instance_id, provider)
);

-- Global and insert-only. SchemaDrift means the adapter is wrong everywhere,
-- so the first instance to notice wins and later writers are no-ops.
create table if not exists provider_quarantine (
  provider    text primary key,
  reason      text not null,
  instance_id text not null,
  at          timestamptz not null default now()
);
