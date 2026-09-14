-- =====================================================================
-- 033_cfbd_player_reference_remove_player_id.sql
--
-- Removes the player_id column from cfbd_player_reference.  The linking
-- between CFBD athletes and fantasy players now happens at roster
-- assignment time via name + college_team matching against
-- cfbd_player_reference (the canonical NCAAF identity table).
--
-- Also adds partial indexes on name and team for the new MATCH pattern:
--   cfbd_player_reference nrm_name ~ player_name
--   cfbd_player_reference nrm_team  ~ college_team
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- The player_id link is no longer needed — roster sync scripts will
-- match by name + college_team directly.
ALTER TABLE cfbd_player_reference DROP COLUMN IF EXISTS player_id;

-- Index for the name-based MATCH in sync_roster_assignments:
--   WHERE cpr.nrm_name = lower(trim(player_name))
CREATE INDEX IF NOT EXISTS idx_cfbd_player_reference_nrm_name
    ON cfbd_player_reference (nrm_name)
    WHERE nrm_name IS NOT NULL;

-- Index for the team-based MATCH:
--   WHERE cpr.nrm_team = lower(trim(college_team))
CREATE INDEX IF NOT EXISTS idx_cfbd_player_reference_nrm_team
    ON cfbd_player_reference (nrm_team)
    WHERE nrm_team IS NOT NULL;

-- Partial unique index on (nrm_name, nrm_team) for fast dedup during INSERT
CREATE INDEX IF NOT EXISTS ix_cfbd_player_reference_name_team
    ON cfbd_player_reference (nrm_name, nrm_team)
    WHERE nrm_name IS NOT NULL AND nrm_team IS NOT NULL;

COMMIT;
