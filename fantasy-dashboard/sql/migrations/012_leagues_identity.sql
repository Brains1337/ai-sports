-- =====================================================================
-- 012_leagues_identity.sql
--
-- Two problems, one root cause.
--
-- 1. UNIQUE(external_league_id) collides across seasons and platforms, so
--    the same league in two seasons cannot coexist.
--
-- 2. external_league_id is BIGINT, but the real Fantrax league ID is
--    "2hbybmp6msnsbuqa". The preflight confirmed someone worked around
--    this by inventing the placeholder 20260201 for league id 10.
--
--    That placeholder is the actual reason sync_fantrax.py resolves its
--    league with `order by id limit 1` — there is no real external id to
--    look up. Fixing the lookup without fixing the column would only move
--    the failure, which is why this migration comes before that rewrite.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/012_leagues_identity.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- 1. Text-native external id, canonical from here on.
-- ---------------------------------------------------------------------
ALTER TABLE leagues ADD COLUMN IF NOT EXISTS external_league_key text;

-- Backfill from the numeric column for the platforms where it was valid.
UPDATE leagues
   SET external_league_key = external_league_id::text
 WHERE external_league_key IS NULL
   AND external_league_id IS NOT NULL;

-- Replace the Fantrax placeholder with the real alphanumeric league ID.
-- Guarded by platform + the known placeholder so it is a no-op on re-run
-- and cannot touch the wrong row.
UPDATE leagues
   SET external_league_key = '2hbybmp6msnsbuqa'
 WHERE platform = 'fantrax-cfb'
   AND external_league_id = 20260201;

-- ---------------------------------------------------------------------
-- 2. Swap the uniqueness constraint.
--    Safe: no FK anywhere references leagues(external_league_id) — every
--    FK targets leagues(id) — and the old global UNIQUE guarantees the new
--    composite cannot collide.
-- ---------------------------------------------------------------------
ALTER TABLE leagues ALTER COLUMN external_league_id DROP NOT NULL;

ALTER TABLE leagues DROP CONSTRAINT IF EXISTS leagues_external_league_id_key;
ALTER TABLE leagues DROP CONSTRAINT IF EXISTS leagues_platform_extkey_season_key;
ALTER TABLE leagues ADD  CONSTRAINT leagues_platform_extkey_season_key
    UNIQUE (platform, external_league_key, season);

-- external_league_key is now required identity, not optional metadata.
ALTER TABLE leagues ALTER COLUMN external_league_key SET NOT NULL;

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT id, platform, season, external_league_id, external_league_key,
--        league_name
--   FROM leagues ORDER BY id;
--
-- Expect league 10 to read:
--   external_league_id = 20260201   (legacy, now derived/ignorable)
--   external_league_key = '2hbybmp6msnsbuqa'
--
-- -- must be empty:
-- SELECT platform, external_league_key, season FROM leagues
--  GROUP BY 1,2,3 HAVING count(*) > 1;

-- =====================================================================
-- NOTES FOR THE SYNC REWRITES
-- =====================================================================
-- All league lookups move to (platform, external_league_key, season).
-- external_league_id is retained only for ESPN/Yahoo convenience and should
-- be treated as derived. Once sync_fantrax.py is rewritten, consider
-- nulling league 10's placeholder id so nothing can key off it by accident.
--
-- The same fix is needed for PLAYER ids: Fantrax player ids are alphanumeric
-- and sync_fantrax.py currently base-36 mangles them into a bigint (lossy,
-- ValueError -> NULL, overflow risk). That is players.external_player_key,
-- added in migration 020.

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- ALTER TABLE leagues DROP CONSTRAINT IF EXISTS leagues_platform_extkey_season_key;
-- ALTER TABLE leagues ALTER COLUMN external_league_key DROP NOT NULL;
-- ALTER TABLE leagues ADD CONSTRAINT leagues_external_league_id_key
--     UNIQUE (external_league_id);
-- ALTER TABLE leagues ALTER COLUMN external_league_id SET NOT NULL;
-- ALTER TABLE leagues DROP COLUMN IF EXISTS external_league_key;
-- COMMIT;
