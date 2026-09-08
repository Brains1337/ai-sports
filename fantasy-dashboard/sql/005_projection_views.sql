-- 004_projection_views.sql
-- Drops and recreates league_rosters + waiver_wire with explicit
-- platform→source_name mapping so projections JOIN correctly.
-- Run: psql -U fantasy -d fantasy -f 004_projection_views.sql

-- Platform → source_name mapping:
--   yahoo-cfb   → cfbd_cfb_proj_yahoo
--   fantrax-cfb → cfbd_cfb_proj_fantrax  (future)
--   espn-nfl    → fantasypros_proj
--   yahoo-nfl   → fantasypros_proj

DROP VIEW IF EXISTS league_rosters CASCADE;
DROP VIEW IF EXISTS waiver_wire CASCADE;

CREATE VIEW league_rosters AS
SELECT
    l.id              AS league_id,
    l.platform,
    l.league_name,
    l.sport,
    rsl.fantasy_team,
    rsl.fantasy_team = (l.payload->>'my_team_name') AS is_my_team,
    p.player_name,
    p.pos,
    p.external_player_id,
    rsl.roster_status,
    pr.projected_points,
    pr.source_name,
    rsl.fetched_at
FROM roster_status_latest rsl
JOIN players p ON p.id = rsl.player_id
JOIN leagues l ON l.id = rsl.league_id
LEFT JOIN projections pr
       ON pr.player_id  = rsl.player_id
      AND pr.source_name = CASE l.platform
            WHEN 'yahoo-cfb'   THEN 'cfbd_cfb_proj_yahoo'
            WHEN 'fantrax-cfb' THEN 'cfbd_cfb_proj_fantrax'
            WHEN 'espn-nfl'    THEN 'fantasypros_proj'
            WHEN 'yahoo-nfl'   THEN 'fantasypros_proj'
            ELSE NULL
          END
WHERE rsl.roster_status = 'owned'
ORDER BY l.id, rsl.fantasy_team, p.pos, pr.projected_points DESC NULLS LAST;

CREATE VIEW waiver_wire AS
SELECT
    l.id              AS league_id,
    l.platform,
    l.league_name,
    l.sport,
    p.player_name,
    p.pos,
    rsl.roster_status,
    p.percent_owned,
    pr.projected_points,
    pr.source_name,
    rsl.fetched_at
FROM roster_status_latest rsl
JOIN players p ON p.id = rsl.player_id
JOIN leagues l ON l.id = rsl.league_id
LEFT JOIN projections pr
       ON pr.player_id  = rsl.player_id
      AND pr.source_name = CASE l.platform
            WHEN 'yahoo-cfb'   THEN 'cfbd_cfb_proj_yahoo'
            WHEN 'fantrax-cfb' THEN 'cfbd_cfb_proj_fantrax'
            WHEN 'espn-nfl'    THEN 'fantasypros_proj'
            WHEN 'yahoo-nfl'   THEN 'fantasypros_proj'
            ELSE NULL
          END
WHERE rsl.roster_status IN ('free_agent','waivers')
ORDER BY l.id, pr.projected_points DESC NULLS LAST;
