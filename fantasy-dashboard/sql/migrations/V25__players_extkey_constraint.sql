-- =====================================================================
-- 025_players_extkey_constraint.sql
--
-- BACKFILL for production: Migration 013 added `external_player_key`
-- column + `ux_players_platform_extkey` unique INDEX in dev (init.sql)
-- but production never ran 013.  sync_fantrax.py uses
-- `ON CONFLICT ON CONSTRAINT ux_players_platform_extkey` which fails on
-- production with:
--
--   constraint "ux_players_platform_extkey" for table "players"
--   does not exist
--
-- ROOT CAUSE: CREATE UNIQUE INDEX creates an index (catalog: pg_index)
-- but NOT a constraint (catalog: pg_constraint).  PostgreSQL's
-- ON CONFLICT ON CONSTRAINT clause looks up the name in pg_constraint,
-- so a plain index — even with the exact same name — is invisible
-- to it.
--
-- FIX: Create a real UNIQUE CONSTRAINT via ALTER TABLE.  PostgreSQL
-- automatically creates the backing index, so we get both a
-- constraint (for ON CONFLICT) and a unique index (for performance)
-- in one statement.
--
-- Idempotent: uses IF NOT EXISTS / IF EXISTS guards.
-- =====================================================================

BEGIN;

-- Add the column if production doesn't have it (013 never ran).
ALTER TABLE players ADD COLUMN IF NOT EXISTS external_player_key text;

-- Remove any orphaned partial index from a previous attempt that used
-- CREATE UNIQUE INDEX instead of a proper constraint.
DROP INDEX IF EXISTS ix_players_platform_extkey;
DROP INDEX IF EXISTS ux_players_platform_extkey;

-- Drop constraint if it somehow exists (re-run safety).
ALTER TABLE players DROP CONSTRAINT IF EXISTS ux_players_platform_extkey;

-- Create the UNIQUE CONSTRAINT.  PG auto-creates the backing index.
-- We keep it non-partial (no WHERE clause) for simplicity —
-- external_player_key is only populated for Fantrax-sourced rows,
-- and NULLs don't conflict in PostgreSQL unique constraints anyway.
ALTER TABLE players ADD CONSTRAINT ux_players_platform_extkey
    UNIQUE (platform, external_player_key);

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- \d players
-- SELECT conname FROM pg_constraint WHERE conname = 'ux_players_platform_extkey';

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- ALTER TABLE players DROP CONSTRAINT IF EXISTS ux_players_platform_extkey;
-- ALTER TABLE players DROP COLUMN IF EXISTS external_player_key;
-- COMMIT;
