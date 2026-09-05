-- roster_changes_report.sql
-- Quick season-tracking report: what changed since the last sync run.
-- Run via: docker exec -i fantasy-db psql -U fantasy -d fantasy -f roster_changes_report.sql

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

-- Players newly dropped to free agency / waivers (good pickup targets)
-- select * from roster_status_changes where current_status in ('free_agent','waivers') and previous_status = 'owned';

-- Players newly picked up (someone made a move worth reacting to)
-- select * from roster_status_changes where previous_status in ('free_agent','waivers') and current_status = 'owned';
