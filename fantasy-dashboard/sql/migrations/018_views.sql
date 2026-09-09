-- =====================================================================
-- 018_views.sql
--
-- !! RUN IN THE SAME DEPLOY AS 014_lineup_status.sql, IMMEDIATELY AFTER !!
-- 014 reclassifies bench players to roster_status='owned', and
-- league_rosters filters on 'owned'. Until this file runs, bench players
-- are indistinguishable from starters in every view.
--
-- Changes:
--   * projections join is now WEEK-AWARE and scoring-format-aware. Before,
--     it matched on source_name only, so a season-total row satisfied a
--     weekly query and every league got PPR numbers regardless of format.
--   * is_my_team reads the my_team_name COLUMN with a payload fallback.
--   * lineup_status / slot_name exposed.
--   * bye weeks surfaced so a player on bye can't be recommended as a start.
--   * new my_roster and opponent_moves views.
--   * roster_status_changes gains a 'transfer' change_type — it previously
--     derived add/drop purely from fantasy_team NULL transitions, so a
--     player moving owner-to-owner reported 'no_change' and every trade and
--     owner-to-owner waiver claim was invisible.
--
-- CREATE OR REPLACE VIEW can only append columns at the end, and these
-- change column order, so the views must be dropped. league_rosters and
-- waiver_wire both depend on roster_status_latest, hence the drop order.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/018_views.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

DROP VIEW IF EXISTS my_roster;
DROP VIEW IF EXISTS opponent_moves;
DROP VIEW IF EXISTS league_rosters;
DROP VIEW IF EXISTS waiver_wire;
DROP VIEW IF EXISTS roster_status_changes;
DROP VIEW IF EXISTS roster_status_latest;

-- ---------------------------------------------------------------------
-- Base: latest status per (player, league)
-- ---------------------------------------------------------------------
CREATE VIEW roster_status_latest AS
SELECT DISTINCT ON (player_id, league_id)
       player_id, league_id, fantasy_team, roster_status,
       lineup_status, slot_name, "position", fetched_at
  FROM roster_status_history
 ORDER BY player_id, league_id,
          CASE roster_status
              WHEN 'owned'      THEN 0
              WHEN 'waivers'    THEN 1
              WHEN 'free_agent' THEN 2
              ELSE 3
          END,
          fetched_at DESC NULLS LAST;

-- ---------------------------------------------------------------------
-- Rostered players, with week-correct projections
-- ---------------------------------------------------------------------
CREATE VIEW league_rosters AS
SELECT l.id            AS league_id,
       l.platform,
       l.league_name,
       l.sport,
       l.scoring_type,
       rsl.fantasy_team,
       (rsl.fantasy_team IS NOT NULL
        AND rsl.fantasy_team = COALESCE(l.my_team_name,
                                        l.payload ->> 'my_team_name')) AS is_my_team,
       p.id            AS player_id,
       p.player_name,
       p.pos,
       p.external_player_id,
       p.external_player_key,
       p.injury_status,
       p.bye_week,
       rsl.roster_status,
       rsl.lineup_status,
       rsl.slot_name,
       pr.week         AS proj_week,
       pr.projected_points,
       pr.floor_points,
       pr.ceiling_points,
       pr.std_dev,
       pr.opportunity,
       pr.ecr_rank,
       pr.source_name,
       ms.opponent_key,
       ms.is_bye,
       ms.implied_points,
       rsl.fetched_at
  FROM roster_status_latest rsl
  JOIN players p  ON p.id = rsl.player_id
  JOIN leagues l  ON l.id = rsl.league_id
  -- Week-aware: prefer the current week's projection, fall back to the
  -- season-long row (week 0) only when no weekly figure exists.
  LEFT JOIN LATERAL (
        SELECT pj.*
          FROM projections pj
         WHERE pj.player_id   = rsl.player_id
           AND pj.season      = l.season
           AND pj.source_name = CASE l.platform
                   WHEN 'yahoo-cfb'   THEN 'cfbd_cfb_proj_yahoo'
                   WHEN 'fantrax-cfb' THEN 'cfbd_cfb_proj_fantrax'
                   WHEN 'espn-nfl'    THEN 'nflverse_proj'
                   WHEN 'yahoo-nfl'   THEN 'nflverse_proj'
                   WHEN 'fantrax-nfl' THEN 'nflverse_proj'
                   ELSE NULL END
           -- Match the league's own scoring format, not whatever exists.
           AND (pj.scoring_format = l.scoring_type OR pj.scoring_format IS NULL)
         ORDER BY (pj.week = 0), pj.week DESC, pj.fetched_at DESC
         LIMIT 1
  ) pr ON true
  LEFT JOIN matchup_schedule ms
        ON  ms.sport  = l.sport
        AND ms.season = l.season
        AND ms.team_key = (
              SELECT pt.team_abbrev FROM pro_teams pt
               WHERE pt.external_team_id = p.pro_team_id
                 AND pt.season = l.season
               LIMIT 1)
        AND ms.week = COALESCE(pr.week, 0)
 WHERE rsl.roster_status = 'owned';

-- ---------------------------------------------------------------------
-- My roster only — what the engine and MCP server actually query
-- ---------------------------------------------------------------------
CREATE VIEW my_roster AS
SELECT * FROM league_rosters WHERE is_my_team;

-- ---------------------------------------------------------------------
-- Available players
-- ---------------------------------------------------------------------
CREATE VIEW waiver_wire AS
SELECT l.id            AS league_id,
       l.platform,
       l.league_name,
       l.sport,
       l.scoring_type,
       p.id            AS player_id,
       p.player_name,
       p.pos,
       p.percent_owned,
       p.injury_status,
       p.bye_week,
       rsl.roster_status,
       pr.week         AS proj_week,
       pr.projected_points,
       pr.floor_points,
       pr.ceiling_points,
       pr.opportunity,
       pr.ecr_rank,
       pr.source_name,
       ms.opponent_key,
       ms.is_bye,
       ms.implied_points,
       rsl.fetched_at
  FROM roster_status_latest rsl
  JOIN players p ON p.id = rsl.player_id
  JOIN leagues l ON l.id = rsl.league_id
  LEFT JOIN LATERAL (
        SELECT pj.*
          FROM projections pj
         WHERE pj.player_id   = rsl.player_id
           AND pj.season      = l.season
           AND pj.source_name = CASE l.platform
                   WHEN 'yahoo-cfb'   THEN 'cfbd_cfb_proj_yahoo'
                   WHEN 'fantrax-cfb' THEN 'cfbd_cfb_proj_fantrax'
                   WHEN 'espn-nfl'    THEN 'nflverse_proj'
                   WHEN 'yahoo-nfl'   THEN 'nflverse_proj'
                   WHEN 'fantrax-nfl' THEN 'nflverse_proj'
                   ELSE NULL END
           AND (pj.scoring_format = l.scoring_type OR pj.scoring_format IS NULL)
         ORDER BY (pj.week = 0), pj.week DESC, pj.fetched_at DESC
         LIMIT 1
  ) pr ON true
  LEFT JOIN matchup_schedule ms
        ON  ms.sport  = l.sport
        AND ms.season = l.season
        AND ms.team_key = (
              SELECT pt.team_abbrev FROM pro_teams pt
               WHERE pt.external_team_id = p.pro_team_id
                 AND pt.season = l.season
               LIMIT 1)
        AND ms.week = COALESCE(pr.week, 0)
 WHERE rsl.roster_status IN ('free_agent','waivers');

-- ---------------------------------------------------------------------
-- Change detection, now including owner-to-owner transfers
-- ---------------------------------------------------------------------
CREATE VIEW roster_status_changes AS
WITH last_two AS (
    SELECT league_id, player_id, fantasy_team, roster_status, fetched_at,
           row_number() OVER (PARTITION BY league_id, player_id
                              ORDER BY fetched_at DESC) AS rn
      FROM roster_status_history
), cur AS (
    SELECT league_id, player_id, fantasy_team AS current_team,
           roster_status AS current_status, fetched_at AS latest_fetched_at
      FROM last_two WHERE rn = 1
), prev AS (
    SELECT league_id, player_id, fantasy_team AS previous_team,
           roster_status AS previous_status, fetched_at AS previous_fetched_at
      FROM last_two WHERE rn = 2
)
SELECT p.id AS player_id,
       p.player_name,
       p.pos,
       c.league_id,
       pr.previous_team,
       c.current_team,
       pr.previous_status,
       c.current_status,
       pr.previous_fetched_at,
       c.latest_fetched_at,
       CASE
           WHEN pr.previous_team IS NULL AND c.current_team IS NOT NULL
               THEN 'add'
           WHEN pr.previous_team IS NOT NULL AND c.current_team IS NULL
               THEN 'drop'
           -- New: both owned but by different teams. Previously reported
           -- 'no_change', hiding every trade and owner-to-owner claim.
           WHEN pr.previous_team IS NOT NULL AND c.current_team IS NOT NULL
                AND pr.previous_team <> c.current_team
               THEN 'transfer'
           WHEN pr.previous_status IS DISTINCT FROM c.current_status
               THEN 'status_change'
           ELSE 'no_change'
       END AS change_type
  FROM cur c
  LEFT JOIN prev pr ON pr.league_id = c.league_id AND pr.player_id = c.player_id
  JOIN players p ON p.id = c.player_id;

-- ---------------------------------------------------------------------
-- Opponent activity, for alerting
-- ---------------------------------------------------------------------
CREATE VIEW opponent_moves AS
SELECT rsc.*,
       l.league_name,
       l.platform,
       COALESCE(l.my_team_name, l.payload ->> 'my_team_name') AS my_team
  FROM roster_status_changes rsc
  JOIN leagues l ON l.id = rsc.league_id
 WHERE rsc.change_type IN ('add','drop','transfer')
   AND COALESCE(rsc.current_team, rsc.previous_team)
       IS DISTINCT FROM COALESCE(l.my_team_name, l.payload ->> 'my_team_name');

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT league_name, platform, count(*) AS owned,
--        count(projected_points) AS with_proj,
--        count(*) FILTER (WHERE is_my_team) AS mine,
--        count(*) FILTER (WHERE is_bye)     AS on_bye
--   FROM league_rosters GROUP BY 1,2 ORDER BY 2;
--
-- SELECT league_name, lineup_status, count(*)
--   FROM league_rosters GROUP BY 1,2 ORDER BY 1,2;
--
-- SELECT change_type, count(*) FROM roster_status_changes GROUP BY 1 ORDER BY 2 DESC;
-- SELECT league_name, count(*) FROM opponent_moves GROUP BY 1;
--
-- NOTE: with_proj stays 0 for the ESPN leagues until sync_nflverse.py and
-- build_projections.py land — the source_name is now 'nflverse_proj', which
-- nothing writes yet. That is expected, not a regression.

-- =====================================================================
-- ROLLBACK: re-run the previous view definitions from init.sql
-- =====================================================================
