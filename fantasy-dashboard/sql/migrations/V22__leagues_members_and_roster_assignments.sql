-- =====================================================================
-- 022_leagues_members_and_roster_assignments.sql
--
-- New tables for tracking fantasy team ownership:
--
--   leagues_members:    platform member/manager profiles
--                       (Yahoo manager name, email, waiver priority, etc.)
--   league_members:     which member manages which team in which league
--                       (links leagues_members <-> leagues, carries the
--                       fantasy_team name and waiver_priority)
--   roster_assignments: which CFBD athlete (or player) is on which
--                       fantasy team, with valid_from/valid_to dates
--                       for tracking roster moves over time
--
-- This replaces the loose 'fantasy_team' text column in
-- roster_status_history with proper foreign-key relationships.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/022_leagues_members_and_roster_assignments.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- 1. leagues_members — platform member/manager profiles
--    One row per fantasy manager per platform. A manager may participate
--    in multiple leagues and may use different names across leagues.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leagues_members (
    id              bigserial PRIMARY KEY,
    platform        text NOT NULL,        -- yahoo-cfb, fantrax-cfb, espn-nfl
    external_member_key text NOT NULL,   -- Yahoo team_id, Fantrax user id, ESPN team id
    manager_name    text,                -- display name from the platform
    manager_email   text,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_leagues_members_platform_extkey
    ON leagues_members (platform, external_member_key);

-- ---------------------------------------------------------------------
-- 2. league_members — which member manages which team in which league
--    Carries the fantasy_team display name and waiver_priority.
--    A manager may be in multiple leagues; a league team has one manager.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS league_members (
    id              bigserial PRIMARY KEY,
    league_id       bigint NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    member_id       bigint NOT NULL REFERENCES leagues_members(id) ON DELETE CASCADE,
    fantasy_team    text NOT NULL,       -- display name (e.g. "Venables Vengeance")
    waiver_priority integer,              -- 1 = highest priority
    team_slot       integer,              -- 1-based team index in the league
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (league_id, fantasy_team),
    UNIQUE (league_id, team_slot)
);

CREATE INDEX IF NOT EXISTS ix_league_members_league
    ON league_members (league_id);
CREATE INDEX IF NOT EXISTS ix_league_members_member
    ON league_members (member_id);

-- ---------------------------------------------------------------------
-- 3. roster_assignments — athlete -> fantasy_team ownership with history
--    Links CFBD athlete IDs (from cfbd_player_reference) to fantasy teams.
--    Uses valid_from/valid_to for temporal tracking of roster moves.
--    athlete_id is for CFB players; player_id is for NFL platform players.
--    At least one of athlete_id or player_id must be non-NULL.
--    season/sport are denormalized from leagues for query convenience.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roster_assignments (
    id              bigserial PRIMARY KEY,
    league_id       bigint NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    member_id       bigint NOT NULL REFERENCES leagues_members(id) ON DELETE CASCADE,
    athlete_id      text,           -- cfbd_player_reference.athlete_id (CFBD players)
    player_id       bigint,          -- players.id (NFL/fantasy-platform players)
    valid_from      timestamptz NOT NULL DEFAULT now(),
    valid_to        timestamptz,     -- NULL = currently assigned
    roster_status   text NOT NULL,    -- 'owned' | 'waivers' | 'free_agent'
    lineup_status   text,            -- 'starter' | 'bench' | 'ir' | 'taxi'
    slot_name       text,
    source_name     text,            -- which sync populated this row
    season          integer,         -- denormalized from leagues.season
    sport           text,            -- denormalized from leagues.sport
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT roster_assignments_xor_player_chk
        CHECK ((athlete_id IS NOT NULL) OR (player_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_roster_assignments_athlete
    ON roster_assignments (athlete_id, sport, season);
CREATE INDEX IF NOT EXISTS ix_roster_assignments_member
    ON roster_assignments (member_id);
CREATE INDEX IF NOT EXISTS ix_roster_assignments_league
    ON roster_assignments (league_id);
CREATE INDEX IF NOT EXISTS ix_roster_assignments_active
    ON roster_assignments (league_id, athlete_id, player_id)
    WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_roster_assignments_validity
    ON roster_assignments (valid_from DESC, valid_to);

-- ---------------------------------------------------------------------
-- 4. Backfill leagues_members from existing roster_status_history
--    where fantasy_team names already exist in the DB.
--    This seeds the member table from data sync_yahoo.py already collected.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    row_count int;
BEGIN
    -- Only backfill if leagues_members is empty
    SELECT count(*) INTO row_count FROM leagues_members;
    IF row_count = 0 THEN
        INSERT INTO leagues_members (platform, external_member_key, manager_name, payload)
        SELECT DISTINCT
            'yahoo-cfb',
            l.external_league_key || ':' || rsh.fantasy_team,
            rsh.fantasy_team,
            jsonb_build_object('source', 'roster_status_history_backfill')
        FROM roster_status_history rsh
        JOIN leagues l ON l.id = rsh.league_id
        WHERE rsh.fantasy_team IS NOT NULL
          AND l.platform = 'yahoo-cfb'
          -- Filter out scraper artifacts from the old sync_yahoo.py
          AND length(trim(rsh.fantasy_team)) >= 3
          AND rsh.fantasy_team !~ '^\s*[0-9]'
          AND rsh.fantasy_team !~ '&'
          AND rsh.fantasy_team !~ '^[A-Z]{2,4}$'
          AND rsh.fantasy_team !~* '^\s*(Sat|Sun|Mon|Tue|Wed|Thu|Fri|Final|Live|Half|Delay|Bye|1st|2nd|3rd|4th|am|pm)\s*$'
          AND rsh.fantasy_team !~* '^Q[1-4]\s'
          AND rsh.fantasy_team !~* '^(Sat|Sun|Mon|Tue|Wed|Thu|Fri)\s+[0-9]+:[0-9]+'
          AND rsh.fantasy_team !~* '^\s*(W|L)\s+'
          AND rsh.fantasy_team !~ ',\s*(WR|TE|RB|QB|K|DEF)$'
          AND rsh.fantasy_team !~ ',?\s*(Half|Q[1-4])'
          AND rsh.fantasy_team !~* '^Owned\s+[·.]'
          AND rsh.fantasy_team !~* '^Owned\b'
        ON CONFLICT (platform, external_member_key) DO NOTHING;

        RAISE NOTICE 'Backfilled leagues_members from roster_status_history (junk filtered)';
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- 5. Backfill league_members by linking existing fantasy_team names
--    to leagues_members.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    row_count int;
BEGIN
    SELECT count(*) INTO row_count FROM league_members;
    IF row_count = 0 THEN
        INSERT INTO league_members
            (league_id, member_id, fantasy_team, payload)
        SELECT DISTINCT
            l.id,
            lm.id,
            rsh.fantasy_team,
            jsonb_build_object('source', 'roster_status_history_backfill')
        FROM roster_status_history rsh
        JOIN leagues l ON l.id = rsh.league_id
        JOIN leagues_members lm ON lm.platform = 'yahoo-cfb'
            AND lm.external_member_key = l.external_league_key || ':' || rsh.fantasy_team
            AND lm.manager_name = rsh.fantasy_team
        WHERE rsh.fantasy_team IS NOT NULL
          AND l.platform = 'yahoo-cfb'
          -- Filter out scraper artifacts from the old sync_yahoo.py
          AND length(trim(rsh.fantasy_team)) >= 3
          AND rsh.fantasy_team !~ '^\s*[0-9]'
          AND rsh.fantasy_team !~ '&'
          AND rsh.fantasy_team !~ '^[A-Z]{2,4}$'
          AND rsh.fantasy_team !~* '^\s*(Sat|Sun|Mon|Tue|Wed|Thu|Fri|Final|Live|Half|Delay|Bye|1st|2nd|3rd|4th|am|pm)\s*$'
          AND rsh.fantasy_team !~* '^Q[1-4]\s'
          AND rsh.fantasy_team !~* '^(Sat|Sun|Mon|Tue|Wed|Thu|Fri)\s+[0-9]+:[0-9]+'
          AND rsh.fantasy_team !~* '^\s*(W|L)\s+'
          AND rsh.fantasy_team !~ ',\s*(WR|TE|RB|QB|K|DEF)$'
          AND rsh.fantasy_team !~ ',?\s*(Half|Q[1-4])'
          AND rsh.fantasy_team !~* '^Owned\s+[·.]'
          AND rsh.fantasy_team !~* '^Owned\b'
        ON CONFLICT (league_id, fantasy_team) DO NOTHING;

        RAISE NOTICE 'Backfilled league_members from roster_status_history';
    END IF;
END $$;

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT count(*) FROM leagues_members;
-- SELECT count(*) FROM league_members;
-- SELECT count(*) FROM roster_assignments;
--
-- SELECT lm.fantasy_team, lm.waiver_priority, lm.member_id, l.league_name
--   FROM league_members lm JOIN leagues l ON l.id = lm.league_id
--  WHERE l.platform = 'yahoo-cfb' ORDER BY lm.waiver_priority;
--
-- \d leagues_members
-- \d league_members
-- \d roster_assignments
-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- DROP TABLE IF EXISTS roster_assignments;
-- DROP TABLE IF EXISTS league_members;
-- DROP TABLE IF EXISTS leagues_members;
-- COMMIT;
