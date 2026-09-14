-- =====================================================================
-- 033_cfbd_player_reference_remove_player_id.sql
--
-- Removes the player_id column from cfbd_player_reference (if it exists
-- from a prior migration attempt).  The linking between CFBD athletes
-- and fantasy players now happens at roster assignment time via name +
-- college_team matching against cfbd_player_reference.
--
-- This migration is idempotent — safe to run on fresh databases (init.sql
-- already creates the normalized_name, normalized_team columns + indexes)
-- and on databases that may have had player_id from a prior 032 attempt.
-- =====================================================================

BEGIN;

-- The player_id link is no longer needed — roster sync scripts match
-- cfbd_player_reference by normalized_name + normalized_team directly.
ALTER TABLE cfbd_player_reference DROP COLUMN IF EXISTS player_id;

-- Recreate indexes to be safe (init.sql should already have these, but
-- CREATE INDEX IF NOT EXISTS makes this migration self-sufficient).
CREATE INDEX IF NOT EXISTS ix_cfbd_ref_normalized_name
    ON cfbd_player_reference (normalized_name);

CREATE INDEX IF NOT EXISTS ix_cfbd_ref_normalized_team
    ON cfbd_player_reference (normalized_team);

CREATE INDEX IF NOT EXISTS ix_cfbd_ref_name_team
    ON cfbd_player_reference (normalized_name, normalized_team);

COMMIT;
