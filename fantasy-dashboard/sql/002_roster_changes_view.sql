-- 002_roster_changes_view.sql
-- Turns the ad-hoc roster_changes_report.sql query into a stable, named view
-- so the new /roster-changes API endpoint (and any future tooling) can query
-- it directly without re-running a script.
--
-- Apply with:
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -f sql/002_roster_changes_view.sql
--
-- Safe to re-run (CREATE OR REPLACE).

create or replace view public.roster_changes_report as
select
    p.player_name,
    p.pos,
    c.previous_status,
    c.current_status,
    c.previous_team,
    c.current_team,
    c.latest_fetched_at
from roster_status_changes c
join players p on p.id = c.player_id
order by c.latest_fetched_at desc, p.pos, p.player_name;
