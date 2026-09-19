-- =====================================================================
-- V34: Add normalized_name / normalized_team to cfbd_player_reference
--
-- These columns are needed by V33's index creation and by
-- sync_cfbd_player_reference.py / sync_yahoo.py for cross-referencing
-- CFBD athletes to fantasy players by (normalized_name, normalized_team,
-- season).
--
-- On fresh DBs, init.sql creates the table WITH these columns via V21,
-- but on production the table was created by an earlier migration/init.sql
-- run that did NOT include them. V21's CREATE TABLE IF NOT EXISTS skips
-- on production, so we add them here.
-- =====================================================================

BEGIN;

ALTER TABLE cfbd_player_reference
    ADD COLUMN IF NOT EXISTS normalized_name text;
ALTER TABLE cfbd_player_reference
    ADD COLUMN IF NOT EXISTS normalized_team text;

-- Create indexes if they don't already exist (V21 should have, but be safe)
CREATE INDEX IF NOT EXISTS ix_cfbd_ref_normalized_name
    ON cfbd_player_reference (normalized_name);
CREATE INDEX IF NOT EXISTS ix_cfbd_ref_normalized_team
    ON cfbd_player_reference (normalized_team);
CREATE INDEX IF NOT EXISTS ix_cfbd_ref_name_team
    ON cfbd_player_reference (normalized_name, normalized_team);

COMMIT;
