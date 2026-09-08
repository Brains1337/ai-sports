-- Migration 006: delete unknown roster_status_history rows, fix views
-- Run: psql -U fantasy -d fantasy -f 006_clean_unknown_rows_and_views.sql

DELETE FROM roster_status_history WHERE roster_status = 'unknown';

CREATE OR REPLACE VIEW roster_status_latest AS
SELECT DISTINCT ON (player_id, league_id)
    player_id, league_id, fantasy_team, roster_status, position, fetched_at
FROM roster_status_history
ORDER BY player_id, league_id,
    CASE roster_status WHEN 'owned' THEN 0 WHEN 'waivers' THEN 1 WHEN 'free_agent' THEN 2 ELSE 3 END,
    fetched_at DESC NULLS LAST;

CREATE OR REPLACE VIEW league_rosters AS
SELECT
    l.id AS league_id, l.platform, l.league_name, l.sport,
    rsl.fantasy_team,
    rsl.fantasy_team = (l.payload->>'my_team_name') AS is_my_team,
    p.player_name, p.pos, p.external_player_id,
    rsl.roster_status, pr.projected_pts, pr.week, rsl.fetched_at
FROM roster_status_latest rsl
JOIN players p ON p.id = rsl.player_id
JOIN leagues l ON l.id = rsl.league_id
LEFT JOIN projections pr ON pr.player_id = rsl.player_id AND pr.league_id = rsl.league_id
WHERE rsl.roster_status = 'owned'
ORDER BY l.id, rsl.fantasy_team, p.pos, pr.projected_pts DESC NULLS LAST;

CREATE OR REPLACE VIEW waiver_wire AS
SELECT
    l.id AS league_id, l.platform, l.league_name, l.sport,
    p.player_name, p.pos, rsl.roster_status,
    p.percent_owned, pr.projected_pts, pr.week, rsl.fetched_at
FROM roster_status_latest rsl
JOIN players p ON p.id = rsl.player_id
JOIN leagues l ON l.id = rsl.league_id
LEFT JOIN projections pr ON pr.player_id = rsl.player_id AND pr.league_id = rsl.league_id
WHERE rsl.roster_status IN ('free_agent','waivers')
ORDER BY l.id, pr.projected_pts DESC NULLS LAST;
