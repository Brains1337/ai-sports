-- =====================================================================
-- 016_player_events.sql
--
-- Injury / news / depth-chart events, plus injury status directly on
-- players. Needed so the projection model and the local LLM can tell
-- "projected low" apart from "listed as out".
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/016_player_events.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

CREATE TABLE IF NOT EXISTS player_events (
    id          bigserial PRIMARY KEY,
    -- Nullable on purpose: news about a player we haven't matched should
    -- still be stored rather than dropped (common/matching refuses to guess).
    player_id   bigint REFERENCES players(id) ON DELETE CASCADE,
    event_type  text NOT NULL,   -- injury | news | depth_chart | practice | transaction
    severity    text,            -- out | doubtful | questionable | probable | info
    headline    text,
    body        text,
    source_name text NOT NULL,
    source_url  text,
    season      integer,
    week        integer,
    event_at    timestamptz NOT NULL,
    content_hash text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),

    -- NULLS NOT DISTINCT is essential. With default NULL-distinct semantics,
    -- rows for unmatched players (player_id IS NULL) would re-insert on every
    -- poll and re-fire duplicate alerts — the exact bug this key prevents.
    UNIQUE NULLS NOT DISTINCT (player_id, source_name, content_hash)
);

CREATE INDEX IF NOT EXISTS ix_player_events_recent
    ON player_events (player_id, event_at DESC);
CREATE INDEX IF NOT EXISTS ix_player_events_type
    ON player_events (event_type, event_at DESC);

ALTER TABLE player_events DROP CONSTRAINT IF EXISTS player_events_type_chk;
ALTER TABLE player_events ADD  CONSTRAINT player_events_type_chk
    CHECK (event_type IN ('injury','news','depth_chart','practice','transaction'))
    NOT VALID;

-- Injury status directly on players. Note only projections.injury_status
-- existed before, which meant the status vanished whenever a projection
-- row was replaced.
ALTER TABLE players ADD COLUMN IF NOT EXISTS injury_status     text;
ALTER TABLE players ADD COLUMN IF NOT EXISTS injury_updated_at timestamptz;
ALTER TABLE players ADD COLUMN IF NOT EXISTS depth_chart_order integer;

CREATE INDEX IF NOT EXISTS ix_players_injury
    ON players (injury_status) WHERE injury_status IS NOT NULL;

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT event_type, severity, count(*), max(event_at)
--   FROM player_events GROUP BY 1,2 ORDER BY 1,2;
-- SELECT injury_status, count(*) FROM players
--  WHERE injury_status IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;
-- -- unmatched news should be present but small:
-- SELECT count(*) FROM player_events WHERE player_id IS NULL;

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- DROP TABLE IF EXISTS player_events;
-- ALTER TABLE players DROP COLUMN IF EXISTS injury_status,
--                     DROP COLUMN IF EXISTS injury_updated_at,
--                     DROP COLUMN IF EXISTS depth_chart_order;
-- COMMIT;
