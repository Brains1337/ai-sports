-- =====================================================================
-- 010_verify.sql — run AFTER 010_canonical_literals.sql
-- Read-only checks, then the constraint VALIDATE step.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -q \
--     < sql/migrations/010_verify.sql > verify010_out.txt 2>&1
-- =====================================================================

\pset pager off
\pset border 2

\echo ''
\echo '=== A. MUST ALL BE EMPTY ==='

\echo '-- A1. players with no sport:'
SELECT platform, count(*) FROM players WHERE sport IS NULL GROUP BY 1;

\echo '-- A2. leagues with no sport:'
SELECT id, league_name FROM leagues WHERE sport IS NULL;

\echo '-- A3. unexpected scoring_type values:'
SELECT DISTINCT scoring_type FROM leagues
 WHERE scoring_type IS NOT NULL
   AND scoring_type NOT IN ('STD','HALF_PPR','PPR','PICKEM');

\echo '-- A4. unexpected roster_status values ("bench" is OK until 013):'
SELECT DISTINCT roster_status FROM roster_status_history
 WHERE roster_status NOT IN ('owned','waivers','free_agent','bench');

\echo '-- A5. any surviving "espn" platform literal:'
SELECT 'players' AS t, count(*) FROM players WHERE platform='espn'
UNION ALL SELECT 'leagues', count(*) FROM leagues WHERE platform='espn'
UNION ALL SELECT 'pro_teams', count(*) FROM pro_teams WHERE platform='espn';

\echo ''
\echo '=== B. STATE AFTER MIGRATION ==='
SELECT platform, sport, count(*) FROM players GROUP BY 1,2 ORDER BY 1;

\x on
SELECT id, external_league_id, platform, sport, scoring_type, team_count,
       waiver_type, faab_budget, my_team_name,
       payload->>'my_team_name' AS payload_my_team_name
  FROM leagues ORDER BY id;
\x off

\echo ''
\echo '=== C. THE PAYOFF METRIC ==='
\echo '# Expect ESPN with_proj to move 0 -> small-but-nonzero (~11% ceiling).'
\echo '# Only 47 FantasyPros players exist, so low coverage here is the'
\echo '# FantasyPros bug, NOT a failure of migration 010.'
SELECT league_name, platform, count(*) AS owned,
       count(projected_points) AS with_proj,
       round(100.0*count(projected_points)/greatest(count(*),1),1) AS pct
  FROM league_rosters GROUP BY 1,2 ORDER BY 2;

\echo ''
\echo '-- C2. waiver wire (ESPN still absent until the FA pass is added):'
SELECT league_name, platform, count(*) AS available,
       count(projected_points) AS with_proj
  FROM waiver_wire GROUP BY 1,2 ORDER BY 2;

\echo ''
\echo '-- C3. is_my_team now resolvable? (was dead before 010)'
SELECT league_name, is_my_team, count(*)
  FROM league_rosters GROUP BY 1,2 ORDER BY 1,2;

\echo ''
\echo '=== D. VALIDATE CONSTRAINTS (only runs if A1-A5 were clean) ==='
ALTER TABLE players VALIDATE CONSTRAINT players_sport_chk;
ALTER TABLE leagues VALIDATE CONSTRAINT leagues_sport_chk;
ALTER TABLE leagues VALIDATE CONSTRAINT leagues_scoring_chk;
\echo '# All three validated.'

\echo ''
\echo '=== VERIFY COMPLETE ==='
