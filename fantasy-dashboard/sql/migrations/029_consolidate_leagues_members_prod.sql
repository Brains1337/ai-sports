-- =====================================================================
-- 029_consolidate_leagues_members_prod.sql
--  — Idempotency-safe production backfill for the leagues_members
--    schema change introduced in migration 024.
-- — Root cause: Migration 024 was never successfully applied on production.
--   The CI/CD deploy job (line 121-126 of .gitlab-ci.yml) applies migrations
--   with ON_ERROR_STOP=1 but swallows errors via '|| true', so if 024 failed
--   partway through its transaction, the ALTER TABLE changes were rolled back.
--   sync_yahoo_members.py (written against the consolidated schema) then fails with:
--     column "league_id" of relation "leagues_members" does not exist
--
-- FIX: This migration re-applies ONLY the schema changes (columns + indexes),
--   without the data migration logic from league_members (which may have been
--   partially executed or already dropped).  All statements use IF NOT EXISTS /
--   IF EXISTS guards so re-runs are safe.
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- 1. Add league-linking columns to leagues_members (idempotent)
ALTER TABLE leagues_members
    ADD COLUMN IF NOT EXISTS league_id bigint REFERENCES leagues(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS fantasy_team text,
    ADD COLUMN IF NOT EXISTS waiver_priority integer,
    ADD COLUMN IF NOT EXISTS team_slot integer,
    ADD COLUMN IF NOT EXISTS source_name text DEFAULT 'yahoo';

-- 2. Create unique index on (platform, external_member_key, league_id)
--    Named to match init.sql (migration 024 used a different name with 'key'
--    instead of 'extkey'; this creates the canonical one if missing).
CREATE UNIQUE INDEX IF NOT EXISTS ux_leagues_members_platform_extkey_league
    ON leagues_members (platform, external_member_key, league_id);

-- 3. Index on league_id for fast roster_assignments joins
CREATE INDEX IF NOT EXISTS ix_leagues_members_league
    ON leagues_members (league_id);

-- 4. Drop the now-redundant league_members table if it still exists
--    (migration 024 step 7 dropped it; this is a safety net for failed runs)
DROP TABLE IF EXISTS league_members CASCADE;
DROP SEQUENCE IF EXISTS league_members_id_seq CASCADE;

COMMIT;

-- =====================================================================
-- VERIFICATION (run manually on production):
--
--   \d leagues_members
--   SELECT conname, contype FROM pg_constraint
--     WHERE conname LIKE '%leagues_members%' AND connamespace = 'public'::regnamespace;
--   SELECT indexname FROM pg_indexes WHERE tablename = 'leagues_members';
--   SELECT count(*) FROM leagues_members WHERE league_id IS NULL;
--
-- =====================================================================
