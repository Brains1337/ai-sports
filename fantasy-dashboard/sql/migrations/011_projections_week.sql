-- =====================================================================
-- 011_projections_week.sql
--
-- Adds the week dimension and makes projections idempotent. This is the
-- prerequisite for rewriting sync_fantasypros.py, which is the highest
-- value fix outstanding (only 47 NFL players are loaded today).
--
-- Preflight note: dupe_factor was 1.00 for all three sources and the
-- dedupe DELETE was measured at 0 rows, so this migration is effectively
-- additive against current data. The dedupe stays in for safety.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/011_projections_week.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- 1. New columns
--    week: 0 = season-long (matches FantasyPros' own sentinel), 1..15 = game week
--    floor/ceiling/std_dev: required by the knockout survival model, which
--      optimizes P(not lowest score) rather than expected points and
--      therefore cannot work from a point estimate alone.
-- ---------------------------------------------------------------------
ALTER TABLE projections ADD COLUMN IF NOT EXISTS week           integer NOT NULL DEFAULT 0;
ALTER TABLE projections ADD COLUMN IF NOT EXISTS floor_points   numeric(10,2);
ALTER TABLE projections ADD COLUMN IF NOT EXISTS ceiling_points numeric(10,2);
ALTER TABLE projections ADD COLUMN IF NOT EXISTS std_dev        numeric(10,2);
ALTER TABLE projections ADD COLUMN IF NOT EXISTS opportunity    numeric(10,2);
ALTER TABLE projections ADD COLUMN IF NOT EXISTS confidence     numeric(4,3);

-- Stop encoding ECR as text in news_summary (currently written as
-- "ECR=12, tier=3, position=RB" then re-parsed by build_rankings.py).
ALTER TABLE projections ADD COLUMN IF NOT EXISTS ecr_rank integer;
ALTER TABLE projections ADD COLUMN IF NOT EXISTS ecr_tier integer;

-- ---------------------------------------------------------------------
-- 2. Collapse duplicates, keeping the freshest row per key.
--    `week` was just added with DEFAULT 0, so every pre-existing row sits
--    in the season bucket — correct, since none of them were week-specific.
--    Ordering is strict and total per key (fetched_at, then id as tiebreak),
--    so exactly one row survives.
-- ---------------------------------------------------------------------
DELETE FROM projections p USING projections q
WHERE p.player_id   = q.player_id
  AND p.source_name = q.source_name
  AND p.season      = q.season
  AND p.week        = q.week
  AND coalesce(p.scoring_format,'') = coalesce(q.scoring_format,'')
  AND (p.fetched_at < q.fetched_at
       OR (p.fetched_at = q.fetched_at AND p.id < q.id));

-- ---------------------------------------------------------------------
-- 3. The upsert key.
--    coalesce() on scoring_format because the column is nullable and NULLs
--    are distinct by default, which would let duplicate NULL-format rows
--    slip past. A bare COALESCE is legal as an index element and IMMUTABLE.
--
--    Conflict targets must spell the expression out in full:
--      ON CONFLICT (player_id, source_name, season, week,
--                   coalesce(scoring_format,'')) DO UPDATE ...
--    `ON CONFLICT ux_projections_key` is NOT valid syntax.
--
--    No separate lookup index: (player_id, source_name, season, week) is a
--    strict leading prefix of this one.
-- ---------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_projections_key
    ON projections (player_id, source_name, season, week,
                    coalesce(scoring_format,''));

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT source_name, season, week, scoring_format, count(*) AS rows,
--        count(DISTINCT player_id) AS players, max(fetched_at) AS newest
--   FROM projections GROUP BY 1,2,3,4 ORDER BY 1,2,3,4;
--
-- -- must be empty (proves the unique key holds):
-- SELECT player_id, source_name, season, week, coalesce(scoring_format,'')
--   FROM projections GROUP BY 1,2,3,4,5 HAVING count(*) > 1;

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- DROP INDEX IF EXISTS ux_projections_key;
-- ALTER TABLE projections
--     DROP COLUMN IF EXISTS week,
--     DROP COLUMN IF EXISTS floor_points,
--     DROP COLUMN IF EXISTS ceiling_points,
--     DROP COLUMN IF EXISTS std_dev,
--     DROP COLUMN IF EXISTS opportunity,
--     DROP COLUMN IF EXISTS confidence,
--     DROP COLUMN IF EXISTS ecr_rank,
--     DROP COLUMN IF EXISTS ecr_tier;
-- COMMIT;
