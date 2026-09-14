-- =====================================================================
-- 032_cfbd_player_reference_player_id.sql
--
-- Adds a `player_id` column to cfbd_player_reference that stores the
-- FK link to players.id when the xref script finds a match for a
-- CFBD athlete against platform players.
--
-- This replaces the old design where sync_cfbd_player_xref.py wrote
-- cfbd_athlete_id into players.payload and roster assignment queries
-- read it back from there.  Now roster assignment queries JOIN
-- players → cfbd_player_reference ON players.id = cfbd_player_reference.player_id
-- to obtain athlete_id directly from the canonical CFBD table.
--
-- Also adds an index on (player_id) for fast roster assignment JOINs.
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- Link to the canonical players table.  Nullable because most CFBD
-- athletes won't have a matching fantasy league player.
ALTER TABLE cfbd_player_reference
    ADD COLUMN IF NOT EXISTS player_id bigint REFERENCES players(id) ON DELETE SET NULL;

COMMENT ON COLUMN cfbd_player_reference.player_id IS
    'FK to players.id when a CFBD athlete is matched to a platform player. '
    || 'Used by sync_yahoo_members.py and sync_fantrax_members.py to resolve '
    || 'athlete_id in roster_assignments without touching players.payload.';

-- Index for the roster assignment JOIN pattern:
--   join cfbd_player_reference cpr on p.id = cpr.player_id
CREATE INDEX IF NOT EXISTS idx_cfbd_player_reference_player_id
    ON cfbd_player_reference (player_id)
    WHERE player_id IS NOT NULL;

COMMIT;
