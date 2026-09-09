-- =====================================================================
-- 009_preflight.sql — READ-ONLY. Writes nothing, locks nothing.
--
-- Purpose: init.sql is a schema-only dump, so we have no visibility into
-- what values are actually stored. Migration 010 is a data rewrite and two
-- of its statements can abort on a unique-constraint collision depending
-- on what's in here. Run this first and send the output.
--
-- Usage (from fantasy-dashboard/ on llm-sports-p01):
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -q \
--     < sql/preflight/009_preflight.sql > preflight_out.txt 2>&1
-- =====================================================================

\pset pager off
\pset border 2
\timing off

\echo ''
\echo '################ 1. PLATFORM LITERALS ################'
\echo '# Migration 010 only renames the literal "espn". If anything here is'
\echo '# bare "yahoo" / "fantrax" / "espn-nfl", 010 needs more UPDATEs.'
SELECT 'players'   AS tbl, platform, count(*) AS row_count FROM players   GROUP BY 1,2
UNION ALL
SELECT 'leagues'   AS tbl, platform, count(*)         FROM leagues   GROUP BY 1,2
UNION ALL
SELECT 'pro_teams' AS tbl, platform, count(*)         FROM pro_teams GROUP BY 1,2
ORDER BY 1,2;

\echo ''
\echo '################ 2. RENAME COLLISION CHECKS ################'
\echo '# ALL THREE MUST RETURN ZERO ROWS. Any output = 010 will abort.'

\echo '-- 2a. players, non-null external ids already split across both literals:'
SELECT external_player_id, count(*) AS dupes
  FROM players
 WHERE platform IN ('espn','espn-nfl') AND external_player_id IS NOT NULL
 GROUP BY 1 HAVING count(DISTINCT platform) > 1;

\echo '-- 2b. players, NULL-id rows that would collide on (platform, name, pos):'
SELECT player_name, pos, count(*) AS dupes
  FROM players
 WHERE platform IN ('espn','espn-nfl') AND external_player_id IS NULL
 GROUP BY 1,2 HAVING count(*) > 1;

\echo '-- 2c. pro_teams collision on (platform, external_team_id, season):'
SELECT external_team_id, season, count(*) AS dupes
  FROM pro_teams
 WHERE platform IN ('espn','espn-nfl')
 GROUP BY 1,2 HAVING count(DISTINCT platform) > 1;

\echo ''
\echo '################ 3. EXISTING VOCABULARIES ################'
\echo '-- 3a. players.sport (expect NULL for espn + fantrax, NCAAF for yahoo):'
SELECT sport, count(*) FROM players GROUP BY 1 ORDER BY 2 DESC;

\echo '-- 3b. players.pos — checking the DEF vs D/ST split and NULL positions:'
SELECT platform, pos, count(*) FROM players GROUP BY 1,2 ORDER BY 1, 3 DESC;

\echo '-- 3c. roster_status values, and whether "unknown" rows carry a team:'
SELECT roster_status, (fantasy_team IS NULL) AS team_is_null, count(*)
  FROM roster_status_history GROUP BY 1,2 ORDER BY 1,2;

\echo ''
\echo '################ 4. THE LEAGUES TABLE (full dump) ################'
\echo '# CRITICAL: external_league_id is BIGINT, but your Fantrax league ID is'
\echo '# "2hbybmp6msnsbuqa" — alphanumeric. It CANNOT be stored in that column.'
\echo '# Expecting to see the Fantrax row missing, or present with a bogus id.'
\x on
SELECT id, external_league_id, platform, season, league_name, sport,
       scoring_type, team_count, teams_joined, waiver_type, faab_budget,
       my_team_name, my_team_external_id,
       payload->>'my_team_name' AS payload_my_team_name,
       (SELECT count(*) FROM jsonb_object_keys(payload)) AS payload_key_count
  FROM leagues ORDER BY id;
\x off

\echo ''
\echo '################ 5. LEAGUE SLOTS ################'
\echo '# Compare against the settings you sent. Expect these to be wrong/absent.'
SELECT l.id, l.platform, l.league_name, ls.slot_name, ls.slot_count
  FROM leagues l LEFT JOIN league_slots ls ON ls.league_id = l.id
 ORDER BY l.id, ls.slot_name;

\echo ''
\echo '################ 6. DATA VOLUME PER LEAGUE ################'
\echo '# Which leagues are actually being ingested at all?'
SELECT l.id, l.platform, l.league_name,
       count(DISTINCT rsh.player_id)  AS distinct_players,
       count(*)                       AS history_rows,
       max(rsh.fetched_at)            AS last_sync,
       count(DISTINCT rsh.fantasy_team) AS distinct_teams
  FROM leagues l
  LEFT JOIN roster_status_history rsh ON rsh.league_id = l.id
 GROUP BY 1,2,3 ORDER BY 1;

\echo ''
\echo '-- 6b. Orphan history rows with no league (these never dedupe / never join):'
SELECT count(*) AS orphan_history_rows
  FROM roster_status_history WHERE league_id IS NULL;

\echo ''
\echo '################ 7. PROJECTIONS STATE ################'
\echo '-- 7a. What sources exist, how stale, and how badly duplicated:'
SELECT source_name, season, scoring_format,
       count(*)                        AS rows,
       count(DISTINCT player_id)       AS distinct_players,
       round(count(*)::numeric / greatest(count(DISTINCT player_id),1), 2) AS dupe_factor,
       min(fetched_at)                 AS oldest,
       max(fetched_at)                 AS newest
  FROM projections
 GROUP BY 1,2,3 ORDER BY 1,2,3;

\echo '-- 7b. How many rows will migration 011 delete as duplicates?'
SELECT count(*) AS row_count_011_will_delete
  FROM projections p
 WHERE EXISTS (
   SELECT 1 FROM projections q
    WHERE q.player_id = p.player_id
      AND q.source_name = p.source_name
      AND q.season = p.season
      AND coalesce(q.scoring_format,'') = coalesce(p.scoring_format,'')
      AND (q.fetched_at > p.fetched_at
           OR (q.fetched_at = p.fetched_at AND q.id > p.id)));

\echo ''
\echo '################ 8. THE PAYOFF METRIC ################'
\echo '# Projection coverage per league TODAY. This is the number migration 010'
\echo '# is supposed to fix. Expect with_proj = 0 for the two ESPN leagues.'
SELECT league_name, platform, count(*) AS owned_players,
       count(projected_points) AS with_proj
  FROM league_rosters GROUP BY 1,2 ORDER BY 2;

\echo ''
\echo '-- 8b. Same for the waiver wire:'
SELECT league_name, platform, count(*) AS available_players,
       count(projected_points) AS with_proj
  FROM waiver_wire GROUP BY 1,2 ORDER BY 2;

\echo ''
\echo '################ 9. XREF / RANKINGS / RECS COVERAGE ################'
SELECT 'player_xref' AS tbl, source_name, count(*),
       round(avg(confidence)::numeric,3) AS avg_conf
  FROM player_xref GROUP BY 1,2
UNION ALL
SELECT 'players w/ cfbd_athlete_id', platform, count(*), NULL
  FROM players WHERE payload ? 'cfbd_athlete_id' GROUP BY 1,2
ORDER BY 1,2;

\echo '-- 9b. rankings / derived_rankings / recommendation tables:'
SELECT 'rankings' AS tbl, count(*) AS row_count, max(created_at) AS newest FROM rankings
UNION ALL SELECT 'derived_rankings', count(*), max(created_at) FROM derived_rankings
UNION ALL SELECT 'start_sit_recommendations', count(*), max(created_at) FROM start_sit_recommendations
UNION ALL SELECT 'waiver_targets', count(*), max(created_at) FROM waiver_targets
UNION ALL SELECT 'drop_candidates', count(*), max(created_at) FROM drop_candidates
UNION ALL SELECT 'draft_rules', count(*), max(created_at) FROM draft_rules
ORDER BY 1;

\echo '-- 9c. rankings duplicate check (the build_rankings N-leagues bug):'
SELECT sport, scoring_type, week, count(*) AS row_count,
       count(DISTINCT player_id) AS distinct_players
  FROM rankings GROUP BY 1,2,3 ORDER BY 1,2,3;

\echo ''
\echo '################ 10. SIZES & SYNC FRESHNESS ################'
SELECT c.relname AS tbl, s.n_live_tup AS est_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
  FROM pg_stat_user_tables s
  JOIN pg_class c ON c.oid = s.relid
 ORDER BY pg_total_relation_size(c.oid) DESC;

\echo '-- 10b. sources audit log — what has actually fetched successfully:'
SELECT source_name, status, count(*), max(fetched_at) AS newest
  FROM sources GROUP BY 1,2 ORDER BY 1,2;

\echo ''
\echo '################ 11. SCHEMA DRIFT CHECK ################'
\echo '# Confirms the live DB matches init.sql before we migrate against it.'
SELECT table_name, column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_schema = 'public'
   AND (table_name::text, column_name::text) IN (
        ('leagues','external_league_id'), ('leagues','sport'),
        ('leagues','scoring_type'), ('leagues','my_team_name'),
        ('players','sport'), ('players','external_player_id'),
        ('players','injury_status'), ('players','name_norm'),
        ('projections','week'), ('projections','scoring_format'),
        ('pro_teams','platform'), ('rankings','league_id'),
        ('roster_status_history','league_id'), ('roster_status_history','lineup_status'))
 ORDER BY table_name, column_name;

\echo '-- 11b. Do the tables/views we assume are missing actually not exist?'
SELECT c.relname, c.relkind
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname='public' AND c.relkind IN ('r','v','m')
 ORDER BY c.relkind, c.relname;

\echo ''
\echo '################ PREFLIGHT COMPLETE ################'
