-- =====================================================================
-- 015_matchup_schedule.sql
--
-- Weekly opponent, bye flag, Vegas line and opponent defensive strength.
--
-- Two things this unblocks:
--   1. build_rankings.py currently inserts opp_team and def_strength as
--      hardcoded NULL, so every opponent-aware code path downstream is
--      unreachable.
--   2. Bye weeks. Right now a player on bye can be recommended as a start,
--      because nothing knows he isn't playing.
--
-- implied_points = total/2 - spread/2 is the single most predictive
-- team-level input for fantasy scoring, because it prices in injuries,
-- weather and matchup automatically. nfl_data_py's import_schedules()
-- carries spread_line and total_line, so this costs no extra API access.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/015_matchup_schedule.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

CREATE TABLE IF NOT EXISTS matchup_schedule (
    id             bigserial PRIMARY KEY,
    sport          text    NOT NULL,
    season         integer NOT NULL,
    week           integer NOT NULL,
    -- Canonical team identity (normalized abbreviation, e.g. 'KC', 'OSU').
    -- Deliberately text rather than an FK: NFL and NCAAF teams come from
    -- different providers and pro_teams is ESPN-shaped only.
    team_key       text    NOT NULL,
    opponent_key   text,
    is_home        boolean,
    is_bye         boolean NOT NULL DEFAULT false,
    kickoff_at     timestamptz,
    game_key       text,

    -- Vegas. spread is from THIS team's perspective: negative = favored.
    vegas_spread   numeric(6,2),
    vegas_total    numeric(6,2),
    implied_points numeric(6,2),

    -- Opponent defensive strength, for the matchup adjustment.
    opp_def_rank_overall numeric(6,2),
    opp_def_ppa_pass     numeric(8,4),
    opp_def_ppa_rush     numeric(8,4),
    opp_pts_allowed_avg  numeric(6,2),

    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (sport, season, week, team_key)
);

-- No extra lookup index: the UNIQUE constraint's implicit index already
-- covers (sport, season, week, team_key) exactly.

-- Derive implied_points automatically so ingestion can't forget it and the
-- arithmetic lives in one place.
CREATE OR REPLACE FUNCTION matchup_set_implied() RETURNS trigger AS $$
BEGIN
    IF NEW.vegas_total IS NOT NULL AND NEW.vegas_spread IS NOT NULL THEN
        NEW.implied_points := (NEW.vegas_total / 2.0) - (NEW.vegas_spread / 2.0);
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_matchup_set_implied ON matchup_schedule;
CREATE TRIGGER trg_matchup_set_implied
    BEFORE INSERT OR UPDATE ON matchup_schedule
    FOR EACH ROW EXECUTE FUNCTION matchup_set_implied();

COMMIT;

-- =====================================================================
-- VERIFICATION (after sync_nflverse.py has run)
-- =====================================================================
-- SELECT sport, season, week, count(*) AS teams,
--        count(vegas_total) AS with_line,
--        count(*) FILTER (WHERE is_bye) AS byes
--   FROM matchup_schedule GROUP BY 1,2,3 ORDER BY 1,2,3;
--
-- -- sanity: favored teams should have the higher implied total
-- SELECT team_key, opponent_key, vegas_spread, vegas_total, implied_points
--   FROM matchup_schedule
--  WHERE sport='NFL' AND week=1 ORDER BY implied_points DESC NULLS LAST LIMIT 10;

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- DROP TRIGGER IF EXISTS trg_matchup_set_implied ON matchup_schedule;
-- DROP FUNCTION IF EXISTS matchup_set_implied();
-- DROP TABLE IF EXISTS matchup_schedule;
-- COMMIT;
