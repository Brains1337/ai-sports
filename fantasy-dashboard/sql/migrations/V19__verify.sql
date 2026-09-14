-- =====================================================================
-- 019_verify.sql — run AFTER 010_canonical_literals.sql
-- Verification checks and constraint validation step.
--
-- NOTE: The \pset/\echo/\x psql meta-commands from the original script
-- have been removed for Flyway JDBC compatibility. The SELECT checks
-- below will only error if a constraint is violated — the original
-- "must be empty" verifiers are now implicit in the VALIDATE CONSTRAINT
-- calls at the end.
-- =====================================================================

-- A. MUST ALL BE EMPTY (these SELECTs will return rows if data is bad)
-- A1. players with no sport:
SELECT platform, count(*) FROM players WHERE sport IS NULL GROUP BY 1;

-- A2. leagues with no sport:
SELECT id, league_name FROM leagues WHERE sport IS NULL;

-- A3. unexpected scoring_type values:
SELECT DISTINCT scoring_type FROM leagues
 WHERE scoring_type IS NOT NULL
   AND scoring_type NOT IN ('STD','HALF_PPR','PPR','PICKEM');

-- A4. unexpected roster_status values ("bench" is OK until 013):
SELECT DISTINCT roster_status FROM roster_status_history
 WHERE roster_status NOT IN ('owned','waivers','free_agent','bench');

-- A5. any surviving "espn" platform literal:
SELECT 'players' AS t, count(*) FROM players WHERE platform='espn'
UNION ALL SELECT 'leagues', count(*) FROM leagues WHERE platform='espn'
UNION ALL SELECT 'pro_teams', count(*) FROM pro_teams WHERE platform='espn';

-- B. STATE AFTER MIGRATION
SELECT platform, sport, count(*) FROM players GROUP BY 1,2 ORDER BY 1,2;

SELECT id, external_league_id, platform, sport, scoring_type, team_count,
       waiver_type, faab_budget, my_team_name,
       payload->>'my_team_name' AS payload_my_team_name
  FROM leagues ORDER BY id;

-- C. THE PAYOFF METRIC
SELECT league_name, platform, count(*) AS owned,
       count(projected_points) AS with_proj,
       round(100.0*count(projected_points)/greatest(count(*),1),1) AS pct
  FROM league_rosters GROUP BY 1,2 ORDER BY 2;

-- C2. waiver wire (ESPN still absent until the FA pass is added):
SELECT league_name, platform, count(*) AS available,
       count(projected_points) AS with_proj
  FROM waiver_wire GROUP BY 1,2 ORDER BY 2;

-- C3. is_my_team now resolvable? (was dead before 010)
SELECT league_name, is_my_team, count(*)
  FROM league_rosters GROUP BY 1,2 ORDER BY 1,2;

-- D. VALIDATE CONSTRAINTS (only runs if A1-A5 were clean)
ALTER TABLE players VALIDATE CONSTRAINT players_sport_chk;
ALTER TABLE leagues VALIDATE CONSTRAINT leagues_sport_chk;
ALTER TABLE leagues VALIDATE CONSTRAINT leagues_scoring_chk;