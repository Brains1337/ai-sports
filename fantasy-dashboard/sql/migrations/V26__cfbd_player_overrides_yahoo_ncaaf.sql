-- =====================================================================
-- 026_cfbd_player_overrides_yahoo_ncaaf.sql
--
-- Manual CFBD athlete_id overrides for Yahoo-CFB players that the
-- automatic name+team matching in sync_cfbd_player_xref.py cannot
-- resolve.  These fall into three buckets:
--
--   1. Abbreviated first names from Yahoo  (e.g. "J. Sagapolutele")
--   2. Ambiguous name-only matches         (e.g. "Thurman" x4)
--   3. Players whose college_team is missing from the Yahoo payload
--      so exact (name, team) matching fails
--
-- The overrides table has PK (platform, player_name, pos), so re-runs
-- are idempotent.  The xref script checks this table BEFORE doing
-- any fuzzy matching.
-- =====================================================================

BEGIN;

-- 1. J. Sagapolutele, QB — Yahoo truncates first name to "J."
--    CFBD has "Jaron-Keawe Sagapolutele" (California, #12) and
--    "Josh Sagapolutele" (Hawai'i, DL).  Yahoo lists this as QB at
--    California, so it's Jaron-Keawe.
INSERT INTO cfbd_player_overrides (platform, player_name, pos, cfbd_athlete_id)
    VALUES ('yahoo-cfb', 'J. Sagapolutele', 'QB', '5164313')
    ON CONFLICT (platform, player_name, pos)
    DO UPDATE SET cfbd_athlete_id = EXCLUDED.cfbd_athlete_id;

-- 2. Jelani Thurman, TE — 4 CFBD "Thurman" matches, team disambiguation fails
--    CFBD: Jelani Thurman, North Carolina, TE
INSERT INTO cfbd_player_overrides (platform, player_name, pos, cfbd_athlete_id)
    VALUES ('yahoo-cfb', 'Jelani Thurman', 'TE', '4871039')
    ON CONFLICT (platform, player_name, pos)
    DO UPDATE SET cfbd_athlete_id = EXCLUDED.cfbd_athlete_id;

-- 3. Jayden Scott, RB — single CFBD match but college_team missing from
--    Yahoo payload causes exact (name, team) match to fail
--    CFBD: Jayden Scott, NC State, RB
INSERT INTO cfbd_player_overrides (platform, player_name, pos, cfbd_athlete_id)
    VALUES ('yahoo-cfb', 'Jayden Scott', 'RB', '5126513')
    ON CONFLICT (platform, player_name, pos)
    DO UPDATE SET cfbd_athlete_id = EXCLUDED.cfbd_athlete_id;

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT platform, player_name, pos, cfbd_athlete_id FROM cfbd_player_overrides;
-- SELECT count(*) AS override_count FROM cfbd_player_overrides;
-- =====================================================================
