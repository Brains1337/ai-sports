-- =====================================================================
-- 021_cfbd_player_reference.sql
--
-- CFBD athlete reference table: one column per field from the CFBD /roster
-- endpoint, which is a single bulk call for all FBS teams in one season.
--
-- The /roster endpoint returns RosterPlayer (fixed schema):
--   id, firstName, lastName, team, height, weight, jersey, position,
--   homeCity, homeState, homeCountry, homeLatitude, homeLongitude,
--   homeCountyFIPS, recruitIds, year (deprecated)
--
-- A second call to /teams/fbs enriches each player with team-level
-- metadata (conference, division, classification, school id, etc.).
--
-- Total: 2 API calls per season. Well within the 30k/month Tier 2 budget.
--
-- The cfbd_athlete_id column on players.payload (written by
-- sync_cfbd_player_xref.py) is the FK that links this table to the
-- fantasy-platform players table.
--
-- Design: NO payload catch-all column. Every CFBD field gets its own
-- column so downstream code queries directly without JSON parsing.
-- Future CFBD fields are added as columns in new migrations.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/021_cfbd_player_reference.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

CREATE TABLE IF NOT EXISTS cfbd_player_reference (
    -- CFBD athlete id (string). Primary key. This is the value stored
    -- as players.payload->>'cfbd_athlete_id' by sync_cfbd_player_xref.py.
    athlete_id       text    NOT NULL,

    -- Identity (from /roster RosterPlayer)
    first_name       text,
    last_name        text,
    full_name        text,
    position         text,            -- CFBD position code (e.g. 'QB', 'RB', 'OL')
    team             text,            -- CFBD team name (e.g. 'Michigan')

    -- Physical (from /roster)
    height           numeric(5,2),    -- feet.inches format (e.g. 6.2 = 6'2")
    weight           integer,
    jersey           integer,

    -- Hometown / recruiting background (from /roster)
    home_city        text,
    home_state       text,
    home_country     text,
    home_latitude    numeric(10,7),
    home_longitude   numeric(10,7),
    home_county_fips text,
    recruit_ids      text,            -- comma-separated CFBD recruiting IDs

    -- Team metadata (from /teams/fbs Team object, joined by team name)
    team_id          integer,         -- CFBD team id
    conference       text,
    division         text,
    classification   text,            -- 'fbs' | 'fcs' | 'ii' | 'ii/iii' | 'iii'
    abbreviation     text,
    school           text,            -- canonical school name from /teams

    -- Season context (the /roster 'year' field is deprecated; we use
    -- this instead to tag what season the snapshot represents)
    season           integer NOT NULL,

    -- Bookkeeping
    fetched_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cfbd_ref_season_athlete
    ON cfbd_player_reference (season, athlete_id);
CREATE INDEX IF NOT EXISTS ix_cfbd_ref_team_season
    ON cfbd_player_reference (team, season);
CREATE INDEX IF NOT EXISTS ix_cfbd_ref_position
    ON cfbd_player_reference (position);
CREATE INDEX IF NOT EXISTS ix_cfbd_ref_name
    ON cfbd_player_reference (last_name, first_name);

-- Track which team roster snapshots we've pulled, so re-runs are no-ops
-- and API call usage is auditable against the Tier 2 30k/month budget.
CREATE TABLE IF NOT EXISTS cfbd_sync_runs (
    id          bigserial PRIMARY KEY,
    season      integer NOT NULL,
    endpoint    text NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    row_count   integer NOT NULL DEFAULT 0,
    call_count  integer NOT NULL DEFAULT 0,
    status      text NOT NULL DEFAULT 'ok',   -- ok | empty | error
    error_text  text,
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (season, endpoint)
);

-- =====================================================================
-- Backfill: convert recruit_ids from text[] to text (comma-separated).
-- The earlier version of this migration created recruit_ids as text[],
-- but SQLAlchemy text() cannot bind Python lists to text[] columns.
-- We store as comma-separated text instead.
-- =====================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'cfbd_player_reference'
          AND column_name = 'recruit_ids'
          AND data_type = 'ARRAY'
    ) THEN
        DROP INDEX IF EXISTS ix_cfbd_ref_recruit_ids;
        ALTER TABLE cfbd_player_reference
            ALTER COLUMN recruit_ids TYPE text USING array_to_string(recruit_ids, ',');
    END IF;
END $$;

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT count(*) FROM cfbd_player_reference WHERE season = 2026;
-- SELECT position, count(*) FROM cfbd_player_reference WHERE season = 2026 GROUP BY 1 ORDER BY 2 DESC;
-- SELECT team, count(*) FROM cfbd_player_reference WHERE season = 2026 GROUP BY 1 ORDER BY 2 DESC;
-- SELECT count(*) FROM cfbd_sync_runs WHERE season = 2026;
--
-- -- verify no NULL payload column (it was removed):
-- SELECT count(*) FROM information_schema.columns
--   WHERE table_name = 'cfbd_player_reference' AND column_name = 'payload';
-- -- should be 0
-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- DROP TABLE IF EXISTS cfbd_sync_runs;
-- DROP TABLE IF EXISTS cfbd_player_reference;
-- COMMIT;
