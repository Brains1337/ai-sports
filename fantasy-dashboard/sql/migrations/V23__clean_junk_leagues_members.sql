-- =====================================================================
-- 023_clean_junk_leagues_members.sql
--
-- The backfill in migration 022 inserted junk from roster_status_history
-- fantasy_team column. The old sync_yahoo.py wrote raw HTML text fragments
-- into fantasy_team (timer text, day names, HTML entities, game status
-- tokens, team abbreviations from the roster table, etc.).
--
-- This migration deletes leagues_members and league_members rows where
-- the fantasy_team/manager_name matches junk patterns.
--
-- Junk patterns observed in production:
--   - Q1 12:06,  Q1 9:59,    — quarter + time fragments
--   - Sat 3:00,  Fri 7:00,   — day + time
--   - Sat, Fri,                  — bare day names
--   - Final W,  Half,           — game status
--   - Delay                       — game delay
--   - Bye                        — bye week indicator
--   - RUTG, MIZZ, RICH, BC, KU  — 2-4 letter team abbreviations (all caps)
--   - @&nbsp;, vs&nbsp;         — HTML entities (already handled in 022,
--                                 but clean up any that slipped through)
--
-- Real team names: "Pete's Posse", "Danforth Joe", "Nil Pipe Dream",
--   "JohnH's Quality Team", "KushieTushie cupcakes", etc. — always contain
--   lowercase letters, spaces, and/or apostrophes.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/023_clean_junk_leagues_members.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- 1. Delete junk league_members entries.
--    A real fantasy team name contains lowercase letters (e.g. "Pete's
--    Posse", "Darth Gator"). Junk entries are either:
--      - All uppercase abbreviations (RUTG, MIZZ, BC, KU)
--      - Game status tokens (Sat, Fri, Final, Half, Delay, Bye, Q1 12:06,)
--      - HTML entities (@&nbsp;, vs&nbsp;)
--      - Time fragments (Sat 3:00, Q1 9:59,)
-- ---------------------------------------------------------------------

-- Delete league_members with junk fantasy_team
DELETE FROM league_members
WHERE fantasy_team IS NULL
   -- Empty or whitespace-only
   OR length(trim(fantasy_team)) < 3
   -- Starts with a digit (Q1 12:06, L W, etc.)
   OR fantasy_team ~ '^\s*[0-9]'
   -- Contains HTML entities
   OR fantasy_team ~ '&'
   -- All-uppercase 2-4 char tokens (team abbrevs: RUTG, MIZZ, RICH, BC, KU)
   OR fantasy_team ~ '^[A-Z]{2,4}$'
   -- Bare game status tokens
   OR fantasy_team ~* '^\s*(Sat|Sun|Mon|Tue|Wed|Thu|Fri|Final|Live|Half|Delay|Bye|1st|2nd|3rd|4th|am|pm)\s*$'
   -- Quarter+time fragments: "Q1 12:06," "Q1 9:59,"
   OR fantasy_team ~* '^Q[1-4]\s'
   -- Day+time fragments: "Sat 3:00" "Fri 7:00" "Sat 6:45"
   OR fantasy_team ~* '^(Sat|Sun|Mon|Tue| Wed|Thu|Fri)\s+[0-9]+:[0-9]+'
   -- Game status + record: "Final W" "W vs" "L W"
   OR fantasy_team ~* '^\s*(W|L)\s+'
   OR fantasy_team ~* '^\s*Final\s+\w'
   -- Position labels leaked: "player,WR" ",WR" ",TE" ",RB"
   OR fantasy_team ~ ',\s*(WR|TE|RB|QB|K|DEF)$'
   -- Game status with comma: "Half," "Q1 12:06,"
   OR fantasy_team ~ ',?\s*(Half|Q[1-4])'
   -- Composite game status: "Owned · Sat" "Owned . Final"
   OR fantasy_team ~* '^Owned\s+[·.]'
   OR fantasy_team ~* '^Owned\b';

-- ---------------------------------------------------------------------
-- 2. Delete junk leagues_members rows (manager_name matches junk)
-- ---------------------------------------------------------------------
DELETE FROM leagues_members
WHERE manager_name IS NULL
   OR length(trim(manager_name)) < 3
   OR manager_name ~ '^\s*[0-9]'
   OR manager_name ~ '&'
   OR manager_name ~ '^[A-Z]{2,4}$'
   OR manager_name ~* '^\s*(Sat|Sun|Mon|Tue|Wed|Thu|Fri|Final|Live|Half|Delay|Bye)\s*$'
   OR manager_name ~* '^Q[1-4]\s'
   OR manager_name ~* '^(Sat|Sun|Mon|Tue|Wed|Thu|Fri)\s+[0-9]+:[0-9]+'
   OR manager_name ~* '^\s*(W|L)\s+'
   OR manager_name ~ ',\s*(WR|TE|RB|QB|K|DEF)$'
   OR manager_name ~ ',?\s*(Half|Q[1-4])'
   OR manager_name ~* '^Owned\s+[·.]'
   OR manager_name ~* '^Owned\b';

-- ---------------------------------------------------------------------
-- 3. Delete orphaned leagues_members (no league_members reference)
-- ---------------------------------------------------------------------
DELETE FROM leagues_members lm
WHERE NOT EXISTS (
    SELECT 1 FROM league_members lmem
    WHERE lmem.member_id = lm.id
);

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT fantasy_team, count(*) FROM league_members GROUP BY 1 ORDER BY 1;
-- SELECT manager_name, count(*) FROM leagues_members GROUP BY 1 ORDER BY 1;
-- -- expect ~12 rows (the real Yahoo EDIT League team names)
--
-- SELECT count(*) FROM leagues_members;
-- SELECT count(*) FROM league_members;
-- =====================================================================
-- ROLLBACK (re-run migration 022 backfill to restore if needed)
-- =====================================================================
