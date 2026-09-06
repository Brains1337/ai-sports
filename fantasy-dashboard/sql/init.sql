-- Core ingestion + model tables

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

-- NOTE: player_xref exists in the live DB but is not defined here.
-- You can add a matching create table block after this section using:
--   \d player_xref
-- as the source of truth in your running database. [cite:180]


-- ────────────────────────────────────────────────────────────────
-- Roster status history + latest + changes (documenting current DB)
-- ────────────────────────────────────────────────────────────────

-- Base table: per-league, per-player snapshots over time.
-- Columns match the live roster_status_history table: [cite:180]
--   league_id, player_id, fantasy_team, roster_status, position,
--   fetched_at, payload.
create table if not exists roster_status_history (
  id bigserial primary key,
  league_id bigint references leagues(id) on delete cascade,
  player_id bigint not null references players(id) on delete cascade,
  fantasy_team text,
  roster_status text not null,           -- 'owned', 'free_agent', 'waivers', 'unknown', etc.
  position text,
  fetched_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb
);

create index if not exists ix_roster_status_history_league_player_time
  on roster_status_history (league_id, player_id, fetched_at desc);

create index if not exists ix_roster_status_history_status
  on roster_status_history (roster_status);


-- View: latest status per (league_id, player_id).
-- Matches existing columns in roster_status_latest: [cite:180]
--   player_id, league_id, fantasy_team, roster_status, position, fetched_at
drop view if exists roster_status_latest;

create view roster_status_latest as
select
  league_id,
  player_id,
  fantasy_team,
  roster_status,
  position,
  fetched_at
from (
  select
    league_id,
    player_id,
    fantasy_team,
    roster_status,
    position,
    fetched_at,
    row_number() over (
      partition by league_id, player_id
      order by fetched_at desc
    ) as rn
  from roster_status_history
) s
where rn = 1;


-- View: previous + current status per (league_id, player_id).
-- Extends existing roster_status_changes to include league_id and
-- matches the columns you saw: [cite:180][cite:177]
--   player_id, league_id, previous_team, current_team,
--   previous_status, current_status, latest_fetched_at
drop view if exists roster_status_changes;

create view roster_status_changes as
with last_two as (
  select
    league_id,
    player_id,
    fantasy_team,
    roster_status,
    fetched_at,
    row_number() over (
      partition by league_id, player_id
      order by fetched_at desc
    ) as rn
  from roster_status_history
),
current_rows as (
  select
    league_id,
    player_id,
    fantasy_team as current_team,
    roster_status as current_status,
    fetched_at as latest_fetched_at
  from last_two
  where rn = 1
),
previous_rows as (
  select
    league_id,
    player_id,
    fantasy_team as previous_team,
    roster_status as previous_status,
    fetched_at as previous_fetched_at
  from last_two
  where rn = 2
)
select
  p.id as player_id,
  c.league_id,
  pr.previous_team,
  c.current_team,
  pr.previous_status,
  c.current_status,
  c.latest_fetched_at
from current_rows c
left join previous_rows pr
  on pr.league_id = c.league_id
 and pr.player_id = c.player_id
join players p
  on p.id = c.player_id;