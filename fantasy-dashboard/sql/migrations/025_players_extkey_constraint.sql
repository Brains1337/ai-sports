-- =====================================================================
-- 025_players_extkey_constraint.sql
--
-- BACKFILL for production: Migration 013 added `external_player_key`
-- column + `ux_players_platform_extkey` unique index in dev (init.sql)
-- but production never ran 013.  sync_fantrax.py uses
-- `ON CONFLICT ON CONSTRAINT ux_players_platform_extkey` which fails on
-- production with:
--
--   constraint "ux_players_platform_extkey" for table "players"
--   does not exist
--
-- This migration backfills missing columns + constraint on production
-- so sync_fantrax.py can upsert players.
--
-- Idempotent: uses IF NOT EXISTS / IF EXISTS guards.
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- Add the column if production doesn't have it (013 never ran).
ALTER TABLE players ADD COLUMN IF NOT EXISTS external_player_key text;

-- Drop-and-recreate pattern guarantees the constraint name is correct
-- even if a partial index without the right name already exists.
DROP INDEX IF EXISTS ix_players_platform_extkey;
DROP INDEX IF EXISTS ux_players_platform_extkey;

CREATE UNIQUE INDEX IF NOT EXISTS ux_players_platform_extkey
    ON players (platform, external_player_key)
    WHERE external_player_key IS NOT NULL;

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT indexname, indexdef
-- FROM pg_indexes
-- WHERE tablename = 'players'
--   AND indexname = 'ux_players_platform_extkey';
--
-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- DROP INDEX IF EXISTS ux_players_platform_extkey;
-- ALTER TABLE players DROP COLUMN IF EXISTS external_player_key;
-- COMMIT;
