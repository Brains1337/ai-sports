-- =====================================================================
-- 014_lineup_status.sql
--
-- Splits "does anyone own this player" from "where does he sit in the
-- lineup". These were one column, which is why Fantrax IR/taxi/minors all
-- collapsed to 'bench' and sync_espn_rosters.py discarded lineupSlotId
-- entirely.
--
-- !! MUST SHIP IN THE SAME DEPLOY AS 018_views.sql !!
-- This migration reclassifies the 1080 'bench' rows to 'owned', and
-- league_rosters filters WHERE roster_status='owned'. Between 014 and 018
-- those players appear on the roster with no way to tell them from
-- starters. 018 is what exposes lineup_status.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/014_lineup_status.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

ALTER TABLE roster_status_history
    ADD COLUMN IF NOT EXISTS lineup_status text,
    ADD COLUMN IF NOT EXISTS slot_name     text;

-- Order matters: tag lineup_status BEFORE overwriting roster_status.
-- Idempotent — after the first pass no roster_status='bench' rows remain,
-- so both statements become no-ops.
UPDATE roster_status_history SET lineup_status = 'bench'
 WHERE lineup_status IS NULL AND roster_status = 'bench';

UPDATE roster_status_history SET roster_status = 'owned'
 WHERE roster_status = 'bench';

-- ---------------------------------------------------------------------
-- Index matching roster_status_latest's ACTUAL sort.
-- The view orders by (player_id, league_id, CASE roster_status ...,
-- fetched_at DESC) — the CASE sits between the keys and fetched_at, so a
-- plain (league_id, player_id, fetched_at) index cannot supply this sort.
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_rsh_latest_sort
    ON roster_status_history (
        player_id, league_id,
        (CASE roster_status WHEN 'owned' THEN 0 WHEN 'waivers' THEN 1
                            WHEN 'free_agent' THEN 2 ELSE 3 END),
        fetched_at DESC
    );

-- Now that 'bench' is gone, the vocabulary can be locked down. Deferred
-- from 010 because adding it there would have rejected writes from the
-- still-running fantrax sync.
ALTER TABLE roster_status_history DROP CONSTRAINT IF EXISTS rsh_roster_status_chk;
ALTER TABLE roster_status_history ADD  CONSTRAINT rsh_roster_status_chk
    CHECK (roster_status IN ('owned','waivers','free_agent')) NOT VALID;

ALTER TABLE roster_status_history DROP CONSTRAINT IF EXISTS rsh_lineup_status_chk;
ALTER TABLE roster_status_history ADD  CONSTRAINT rsh_lineup_status_chk
    CHECK (lineup_status IS NULL
           OR lineup_status IN ('starter','bench','ir','taxi')) NOT VALID;

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT roster_status, lineup_status, count(*)
--   FROM roster_status_history GROUP BY 1,2 ORDER BY 1,2;
-- -- must be empty:
-- SELECT DISTINCT roster_status FROM roster_status_history
--  WHERE roster_status NOT IN ('owned','waivers','free_agent');
-- ALTER TABLE roster_status_history VALIDATE CONSTRAINT rsh_roster_status_chk;
-- ALTER TABLE roster_status_history VALIDATE CONSTRAINT rsh_lineup_status_chk;
--
-- NOTE: 5382 rows are roster_status='owned' with fantasy_team IS NULL —
-- owned by nobody identifiable. Deliberately untouched; that needs a sync
-- fix, not a data fix. Track with:
-- SELECT l.league_name, count(*) FROM roster_status_history r
--   JOIN leagues l ON l.id=r.league_id
--  WHERE r.roster_status='owned' AND r.fantasy_team IS NULL
--  GROUP BY 1 ORDER BY 2 DESC;

-- =====================================================================
-- ROLLBACK  (the bench->owned reclassification is NOT reversible from
-- lineup_status alone once new syncs have written rows; restore from dump)
-- =====================================================================
-- BEGIN;
-- ALTER TABLE roster_status_history
--   DROP CONSTRAINT IF EXISTS rsh_roster_status_chk,
--   DROP CONSTRAINT IF EXISTS rsh_lineup_status_chk;
-- DROP INDEX IF EXISTS ix_rsh_latest_sort;
-- UPDATE roster_status_history SET roster_status='bench'
--  WHERE lineup_status='bench' AND roster_status='owned';
-- ALTER TABLE roster_status_history
--   DROP COLUMN IF EXISTS lineup_status, DROP COLUMN IF EXISTS slot_name;
-- COMMIT;
