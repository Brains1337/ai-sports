-- =====================================================================
-- 010_canonical_literals.sql
--
-- Preflight cleared 2026-09-08: all three collision checks returned zero
-- rows, so the espn -> espn-nfl rename cannot violate a unique constraint.
-- Only the literal 'espn' needs renaming (yahoo-cfb / fantrax-cfb /
-- espn-pickem are already canonical).
--
-- Take a backup first:
--   docker exec fantasy-db pg_dump -U fantasy -d fantasy -Fc > pre010.dump
--
-- Apply:
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/010_canonical_literals.sql
--
-- Rollback is at the bottom of this file (commented out).
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- 1. Platform rename: 'espn' -> 'espn-nfl'
--    Affects leagues(2), players(11616), pro_teams(33).
--    This is what makes the league_rosters / waiver_wire projection CASE
--    match, and reconciles build_rankings.py with waiver_planner.py.
-- ---------------------------------------------------------------------
UPDATE players   SET platform = 'espn-nfl' WHERE platform = 'espn';
UPDATE leagues   SET platform = 'espn-nfl' WHERE platform = 'espn';
UPDATE pro_teams SET platform = 'espn-nfl' WHERE platform = 'espn';

-- pro_teams is the only one of the three carrying a DEFAULT. Without this,
-- the next insert that omits platform silently reintroduces 'espn'.
ALTER TABLE pro_teams ALTER COLUMN platform DROP DEFAULT;

-- ---------------------------------------------------------------------
-- 2. sport backfill
--    Preflight showed 15434 NULLs — essentially all Fantrax (only 997 of
--    16431 fantrax-cfb rows had sport set). That NULL is why
--    sync_cfbd_player_xref.py and sync_cfbd_cfb_projections.py, which both
--    filter sport='NCAAF', have skipped every Fantrax player: the preflight
--    confirmed 0 fantrax-cfb players carry a cfbd_athlete_id.
--    ESPN players already had sport='NFL'.
-- ---------------------------------------------------------------------
UPDATE players SET sport = upper(sport) WHERE sport IS NOT NULL;
UPDATE leagues SET sport = upper(sport) WHERE sport IS NOT NULL;
UPDATE players SET sport = 'NCAAF' WHERE sport IN ('CFB','NCAA','COLLEGE');
UPDATE leagues SET sport = 'NCAAF' WHERE sport IN ('CFB','NCAA','COLLEGE');

-- ELSE sport is load-bearing: without it, any platform lacking a -nfl/-cfb
-- suffix would have its sport nulled out.
UPDATE players SET sport = CASE
    WHEN platform LIKE '%-nfl'    THEN 'NFL'
    WHEN platform LIKE '%-cfb'    THEN 'NCAAF'
    WHEN platform  = 'espn-pickem' THEN 'NFL'
    ELSE sport
END
WHERE sport IS NULL;

UPDATE leagues SET sport = CASE
    WHEN platform LIKE '%-nfl'    THEN 'NFL'
    WHEN platform LIKE '%-cfb'    THEN 'NCAAF'
    WHEN platform  = 'espn-pickem' THEN 'NFL'
    ELSE sport
END
WHERE sport IS NULL;

-- ---------------------------------------------------------------------
-- 3. scoring_type corrections
--    Preflight found BOTH college leagues stored as 'PPR', which is wrong
--    and materially changes projections:
--      Yahoo EDIT League    -> HALF_PPR (0.5 per reception)
--      Fantrax New Freshman -> STD      (0 per reception)
--    Note cfbd_cfb_proj_yahoo projections were already scored as HALF_PPR,
--    so the projection data and the league label disagreed.
-- ---------------------------------------------------------------------
UPDATE leagues SET scoring_type = upper(replace(trim(scoring_type), ' ', '_'))
WHERE scoring_type IS NOT NULL;
UPDATE leagues SET scoring_type = 'HALF_PPR'
WHERE scoring_type IN ('HALF','0.5_PPR','HALFPPR','HALF-PPR');
UPDATE leagues SET scoring_type = 'STD'
WHERE scoring_type IN ('STANDARD','NONE','NON-PPR','NON_PPR');

UPDATE leagues SET scoring_type = 'HALF_PPR' WHERE platform = 'yahoo-cfb';
UPDATE leagues SET scoring_type = 'STD'      WHERE platform = 'fantrax-cfb';

-- ---------------------------------------------------------------------
-- 4. League metadata the syncs never populated
--    Preflight: waiver_type NULL for all 5 leagues; team_count NULL for
--    yahoo / fantrax / pickem. Values from config/leagues.yaml.
-- ---------------------------------------------------------------------
UPDATE leagues SET waiver_type = 'faab'     WHERE platform = 'espn-nfl';
UPDATE leagues SET waiver_type = 'priority' WHERE platform IN ('yahoo-cfb','fantrax-cfb');

UPDATE leagues SET team_count = 16 WHERE platform = 'yahoo-cfb'   AND team_count IS NULL;
UPDATE leagues SET team_count = 10 WHERE platform = 'fantrax-cfb' AND team_count IS NULL;

-- my_team_name is already correct for all four roster leagues; mirror it
-- into payload so the league_rosters view's is_my_team works before 018
-- rewrites that view to read the column directly.
UPDATE leagues
SET payload = jsonb_set(coalesce(payload,'{}'::jsonb), '{my_team_name}',
                        to_jsonb(my_team_name), true),
    updated_at = now()
WHERE my_team_name IS NOT NULL
  AND coalesce(payload->>'my_team_name','') <> my_team_name;

-- ---------------------------------------------------------------------
-- 5. roster_status: retire 'unknown'
--    Preflight: 1894 'unknown' rows, ALL with fantasy_team IS NULL, so they
--    are unowned. These were invisible to both league_rosters (owned only)
--    and waiver_wire (free_agent/waivers only) — stored but unreachable.
--
--    NOT touching the 5382 'owned' rows with NULL fantasy_team; those are
--    genuinely owned but unattributed and need a sync fix, not a data fix.
--    The CHECK on roster_status is deferred to 013, after 'bench' is
--    converted — adding it here would break the running fantrax sync.
-- ---------------------------------------------------------------------
UPDATE roster_status_history SET roster_status = 'free_agent'
WHERE roster_status = 'unknown' AND fantasy_team IS NULL;
UPDATE roster_status_history SET roster_status = 'owned'
WHERE roster_status = 'unknown' AND fantasy_team IS NOT NULL;

-- ---------------------------------------------------------------------
-- 6. Constraints
--    sport is nullable and NULL IN (...) evaluates to NULL, which PASSES a
--    CHECK — the IS NOT NULL clause is what actually enforces the backfill.
--    NOT VALID applies to new writes without scanning existing rows.
-- ---------------------------------------------------------------------
ALTER TABLE players DROP CONSTRAINT IF EXISTS players_sport_chk;
ALTER TABLE players ADD  CONSTRAINT players_sport_chk
    CHECK (sport IS NOT NULL AND sport IN ('NFL','NCAAF')) NOT VALID;

ALTER TABLE leagues DROP CONSTRAINT IF EXISTS leagues_sport_chk;
ALTER TABLE leagues ADD  CONSTRAINT leagues_sport_chk
    CHECK (sport IS NOT NULL AND sport IN ('NFL','NCAAF')) NOT VALID;

ALTER TABLE leagues DROP CONSTRAINT IF EXISTS leagues_scoring_chk;
ALTER TABLE leagues ADD  CONSTRAINT leagues_scoring_chk
    CHECK (scoring_type IS NULL
           OR scoring_type IN ('STD','HALF_PPR','PPR','PICKEM')) NOT VALID;

COMMIT;

-- =====================================================================
-- VERIFICATION — run these next. First four must return ZERO rows.
-- =====================================================================
-- SELECT platform, count(*) FROM players WHERE sport IS NULL GROUP BY 1;
-- SELECT id, league_name FROM leagues WHERE sport IS NULL;
-- SELECT DISTINCT scoring_type FROM leagues WHERE scoring_type IS NOT NULL
--   AND scoring_type NOT IN ('STD','HALF_PPR','PPR','PICKEM');
-- SELECT DISTINCT roster_status FROM roster_status_history
--   WHERE roster_status NOT IN ('owned','waivers','free_agent','bench');
--
-- SELECT platform, count(*) FROM players GROUP BY 1 ORDER BY 1;
-- SELECT id, platform, sport, scoring_type, team_count, waiver_type,
--        faab_budget, my_team_name FROM leagues ORDER BY id;
--
-- The payoff metric. NOTE: with_proj will rise for the two ESPN leagues but
-- will NOT approach owned_players, because only 47 FantasyPros players are
-- loaded. That is a separate bug (see plan section 1.7), not a failure here.
-- SELECT league_name, platform, count(*) AS owned, count(projected_points)
--   AS with_proj FROM league_rosters GROUP BY 1,2 ORDER BY 2;
--
-- Once all of the above is clean:
-- ALTER TABLE players VALIDATE CONSTRAINT players_sport_chk;
-- ALTER TABLE leagues VALIDATE CONSTRAINT leagues_sport_chk;
-- ALTER TABLE leagues VALIDATE CONSTRAINT leagues_scoring_chk;

-- =====================================================================
-- ROLLBACK (platform rename + constraints only; the sport/scoring_type
-- backfills are corrections and should not be reverted)
-- =====================================================================
-- BEGIN;
-- ALTER TABLE players DROP CONSTRAINT IF EXISTS players_sport_chk;
-- ALTER TABLE leagues DROP CONSTRAINT IF EXISTS leagues_sport_chk;
-- ALTER TABLE leagues DROP CONSTRAINT IF EXISTS leagues_scoring_chk;
-- UPDATE players   SET platform = 'espn' WHERE platform = 'espn-nfl';
-- UPDATE leagues   SET platform = 'espn' WHERE platform = 'espn-nfl';
-- UPDATE pro_teams SET platform = 'espn' WHERE platform = 'espn-nfl';
-- ALTER TABLE pro_teams ALTER COLUMN platform SET DEFAULT 'espn';
-- COMMIT;
