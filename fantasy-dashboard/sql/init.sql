create table if not exists sources (
  id bigserial primary key,
  source_name text not null,
  source_key text not null,
  fetched_at timestamptz not null default now(),
  content_hash text,
  status text not null default 'ok',
  meta jsonb not null default '{}'::jsonb
);

create unique index if not exists ux_sources_name_key_fetched
  on sources (source_name, source_key, fetched_at);

create table if not exists leagues (
  id bigserial primary key,
  external_league_id bigint not null unique,
  platform text not null,
  season integer not null,
  league_name text not null,
  scoring_type text,
  player_rank_type text,
  scoring_enhancement_type text,
  team_count integer,
  teams_joined integer,
  draft_type text,
  time_per_selection integer,
  faab_budget integer,
  waiver_type text,
  payload jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists league_slots (
  id bigserial primary key,
  league_id bigint not null references leagues(id) on delete cascade,
  slot_name text not null,
  slot_count integer not null,
  unique (league_id, slot_name)
);

create table if not exists pro_teams (
  id bigserial primary key,
  platform text not null default 'espn',
  external_team_id integer not null,
  season integer not null,
  team_name text,
  team_abbrev text,
  bye_week integer,
  payload jsonb not null default '{}'::jsonb,
  unique (platform, external_team_id, season)
);

create table if not exists players (
  id bigserial primary key,
  platform text not null,
  external_player_id bigint,
  player_name text not null,
  first_name text,
  last_name text,
  pos text,
  default_position_id integer,
  pro_team_id integer,
  bye_week integer,
  eligible_slot_names text,
  percent_owned numeric(8,2),
  payload jsonb not null default '{}'::jsonb,
  unique (platform, external_player_id)
);

create table if not exists projections (
  id bigserial primary key,
  player_id bigint not null references players(id) on delete cascade,
  source_name text not null,
  season integer not null,
  scoring_format text,
  projected_points numeric(10,2),
  adp numeric(10,2),
  receptions numeric(10,2),
  pass_yd numeric(10,2),
  rush_yd numeric(10,2),
  rec_yd numeric(10,2),
  injury_status text,
  news_summary text,
  payload jsonb not null default '{}'::jsonb,
  fetched_at timestamptz not null default now()
);

create table if not exists draft_rules (
  id bigserial primary key,
  league_id bigint not null references leagues(id) on delete cascade,
  rules jsonb not null,
  created_at timestamptz not null default now()
);

create table if not exists derived_rankings (
  id bigserial primary key,
  league_id bigint not null references leagues(id) on delete cascade,
  player_id bigint not null references players(id) on delete cascade,
  source_name text not null,
  adjusted_rank integer,
  adjusted_score numeric(10,2),
  score_delta numeric(10,2),
  notes text,
  created_at timestamptz not null default now(),
  unique (league_id, player_id, source_name, created_at)
);
