-- 002-yahoo-history.sql
-- Roster status history for Yahoo college fantasy season tracking.
-- Run once against the existing 'fantasy' database.

create table if not exists roster_status_history (
    id bigserial primary key,
    league_id bigint references leagues(id) on delete cascade,
    player_id bigint not null references players(id) on delete cascade,
    fantasy_team text,          -- Yahoo owning team name, or 'Free Agent' / 'Waivers'
    roster_status text not null, -- e.g. 'owned', 'waivers', 'free_agent'
    position text,
    fetched_at timestamptz not null default now(),
    payload jsonb not null default '{}'::jsonb
);

create index if not exists ix_roster_history_player_fetched
    on roster_status_history (player_id, fetched_at desc);

create index if not exists ix_roster_history_league_fetched
    on roster_status_history (league_id, fetched_at desc);

-- View: most recent snapshot per player
create or replace view roster_status_latest as
select distinct on (player_id)
    player_id, league_id, fantasy_team, roster_status, position, fetched_at
from roster_status_history
order by player_id, fetched_at desc;

-- View: roster status changes between the two most recent snapshot runs
-- (compares latest fetched_at batch to the one immediately before it)
create or replace view roster_status_changes as
with runs as (
    select distinct fetched_at
    from roster_status_history
    order by fetched_at desc
    limit 2
),
ranked_runs as (
    select fetched_at, row_number() over (order by fetched_at desc) as rn
    from runs
),
latest as (
    select h.* from roster_status_history h
    join ranked_runs r on h.fetched_at = r.fetched_at and r.rn = 1
),
previous as (
    select h.* from roster_status_history h
    join ranked_runs r on h.fetched_at = r.fetched_at and r.rn = 2
)
select
    coalesce(l.player_id, p.player_id) as player_id,
    p.fantasy_team as previous_team,
    l.fantasy_team as current_team,
    p.roster_status as previous_status,
    l.roster_status as current_status,
    l.fetched_at as latest_fetched_at
from latest l
full outer join previous p on l.player_id = p.player_id
where l.fantasy_team is distinct from p.fantasy_team
   or l.roster_status is distinct from p.roster_status;
