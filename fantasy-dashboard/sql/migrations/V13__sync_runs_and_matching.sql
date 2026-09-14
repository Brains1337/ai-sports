-- =====================================================================
-- 013_sync_runs_and_matching.sql
--
-- Prerequisites for the sync_fantasypros.py rewrite.
--
-- NOTE ON NUMBERING: the draft plan had 013 = lineup_status and 017 = ops
-- tables. Reordered because sync_runs and the name-matching columns are
-- needed NOW, and lineup_status must ship together with the view rebuild.
-- Revised sequence:
--   013 sync_runs + matching columns   <- this file
--   014 lineup_status  (+ 018 views, same deploy)
--   015 matchup_schedule
--   016 player_events
--   017 player_week_stats
--   018 views
--   019 rankings per-league
--   020 telegram_alerts + waiver_claims
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/013_sync_runs_and_matching.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- 1. sync_runs — the direct antidote to "every failure is silent".
--    The preflight showed `sources` had rows only for espn and fantasypros;
--    yahoo, fantrax and cfbd logged nothing, which is why 9-day-stale
--    FantasyPros data and a never-populated Fantrax projection source went
--    unnoticed. Every script writes a row here from now on.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_runs (
    id           bigserial PRIMARY KEY,
    script_name  text NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    -- running | ok | empty | error
    --   'empty' is deliberately distinct from 'ok': a script that succeeds
    --   but writes zero rows is the exact failure mode this project has.
    status       text NOT NULL DEFAULT 'running',
    rows_written integer NOT NULL DEFAULT 0,
    rows_skipped integer NOT NULL DEFAULT 0,
    error_text   text,
    meta         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_sync_runs_recent
    ON sync_runs (script_name, started_at DESC);

-- ---------------------------------------------------------------------
-- 2. Normalized name columns for indexed matching.
--
--    The current matcher compares a punctuation-STRIPPED needle against an
--    unstripped haystack:
--        normalize_name('A.J. Brown') = 'aj brown'
--        lower(players.player_name)   = 'a.j. brown'
--    so the exact branch can never fire for any name containing . ' or -.
--    It then falls through to LIKE '%aj%', which matches any name containing
--    that substring, ordered by percent_owned — i.e. it returns the most
--    popular unrelated player. That is why 94 FantasyPros keys collapsed
--    onto 47 arbitrary player rows.
--
--    name_norm : accents folded, punctuation stripped, ws collapsed, lowered
--    name_key  : name_norm with a trailing generational suffix removed
--
--    The translate() call folds accents. It is required, not cosmetic:
--    without it '[^A-Za-z0-9 ]' would DELETE an accented character, turning
--    'José' into 'jos', while the Python side folds it to 'jose' — so every
--    accented name would silently miss the index. translate() is IMMUTABLE
--    (unaccent() is only STABLE and is therefore illegal in a generated
--    column). This exact table is mirrored in common/matching.py as
--    SQL_FOLD_FROM / SQL_FOLD_TO and asserted equal by the test suite.
--
--    PG16 forbids a generated column referencing another generated column,
--    so the expression is inlined twice rather than layered.
-- ---------------------------------------------------------------------
ALTER TABLE players ADD COLUMN IF NOT EXISTS name_norm text
    GENERATED ALWAYS AS (
        btrim(regexp_replace(
            lower(regexp_replace(
                translate(player_name,
                    'áàâäãåāéèêëēíìîïīóòôöõøōúùûüūñçýÿšžÁÀÂÄÃÅĀÉÈÊËĒÍÌÎÏĪÓÒÔÖÕØŌÚÙÛÜŪÑÇÝŸŠŽ',
                    'aaaaaaaeeeeeiiiiiooooooouuuuuncyyszAAAAAAAEEEEEIIIIIOOOOOOOUUUUUNCYYSZ'),
                '[^A-Za-z0-9 ]', '', 'g')),
            '\s+', ' ', 'g'))
    ) STORED;

ALTER TABLE players ADD COLUMN IF NOT EXISTS name_key text
    GENERATED ALWAYS AS (
        btrim(regexp_replace(
            btrim(regexp_replace(
                lower(regexp_replace(
                    translate(player_name,
                        'áàâäãåāéèêëēíìîïīóòôöõøōúùûüūñçýÿšžÁÀÂÄÃÅĀÉÈÊËĒÍÌÎÏĪÓÒÔÖÕØŌÚÙÛÜŪÑÇÝŸŠŽ',
                        'aaaaaaaeeeeeiiiiiooooooouuuuuncyyszAAAAAAAEEEEEIIIIIOOOOOOOUUUUUNCYYSZ'),
                    '[^A-Za-z0-9 ]', '', 'g')),
                '\s+', ' ', 'g')),
            '\s+(jr|sr|ii|iii|iv|v)$', '', 'g'))
    ) STORED;

CREATE INDEX IF NOT EXISTS ix_players_name_norm ON players (name_norm, pos);
CREATE INDEX IF NOT EXISTS ix_players_name_key  ON players (name_key,  pos);
CREATE INDEX IF NOT EXISTS ix_players_platform_sport ON players (platform, sport);

-- ---------------------------------------------------------------------
-- 3. Alphanumeric external player ids (Fantrax).
--    sync_fantrax.py currently does int(external_id, 36), which is lossy,
--    raises ValueError on non-base36 characters, and can overflow bigint.
-- ---------------------------------------------------------------------
ALTER TABLE players ADD COLUMN IF NOT EXISTS external_player_key text;
CREATE UNIQUE INDEX IF NOT EXISTS ux_players_platform_extkey
    ON players (platform, external_player_key)
    WHERE external_player_key IS NOT NULL;

-- ---------------------------------------------------------------------
-- 4. Normalize player_xref.confidence to a 0..1 scale.
--    Preflight showed avg_conf = 88.085, i.e. the existing 94 rows are on a
--    0..100 scale while the schema comment says 0.0-1.0. Pick one: 0..1.
-- ---------------------------------------------------------------------
UPDATE player_xref SET confidence = confidence / 100.0 WHERE confidence > 1.0;

ALTER TABLE player_xref DROP CONSTRAINT IF EXISTS player_xref_confidence_chk;
ALTER TABLE player_xref ADD  CONSTRAINT player_xref_confidence_chk
    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)) NOT VALID;

-- Lets us find many-to-one collapses (several source keys -> one player).
CREATE INDEX IF NOT EXISTS ix_player_xref_player
    ON player_xref (source_name, player_id);

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- -- normalization sanity check on the names that break the old matcher:
-- SELECT player_name, name_norm, name_key FROM players
--  WHERE player_name ~ '[.''-]' AND platform='espn-nfl' LIMIT 15;
--
-- -- the smoking gun: source keys collapsing onto one player_id
-- SELECT player_id, count(*) AS source_keys,
--        string_agg(source_player_name, ' | ' ORDER BY source_player_name)
--   FROM player_xref WHERE source_name='fantasypros'
--  GROUP BY 1 HAVING count(*) > 1 ORDER BY 2 DESC;
--
-- SELECT min(confidence), max(confidence), avg(confidence) FROM player_xref;
-- ALTER TABLE player_xref VALIDATE CONSTRAINT player_xref_confidence_chk;

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- DROP TABLE IF EXISTS sync_runs;
-- DROP INDEX IF EXISTS ix_players_name_norm, ix_players_name_key,
--                      ix_players_platform_sport, ux_players_platform_extkey,
--                      ix_player_xref_player;
-- ALTER TABLE player_xref DROP CONSTRAINT IF EXISTS player_xref_confidence_chk;
-- ALTER TABLE players DROP COLUMN IF EXISTS name_norm,
--                     DROP COLUMN IF EXISTS name_key,
--                     DROP COLUMN IF EXISTS external_player_key;
-- COMMIT;
