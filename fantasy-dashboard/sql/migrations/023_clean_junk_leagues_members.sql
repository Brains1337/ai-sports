-- =====================================================================
-- 023_clean_junk_leagues_members.sql
--
-- The backfill in migration 022 inserted junk from roster_status_history
-- fantasy_team column. The old sync_yahoo.py wrote raw HTML text fragments
-- into fantasy_team (timer text, day names, HTML entities, game status
-- tokens like "Q1 12:06," "Sat 3:00" "@&nbsp;" "pm" "Half," "Final" etc.).
--
-- This migration deletes leagues_members and league_members rows where
-- the fantasy_team/manager_name matches junk patterns.
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/023_clean_junk_leagues_members.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- 1. Delete junk league_members entries.
--    A real fantasy team name:
--      - Is at least 3 characters long
--      - Does NOT start with a digit
--      - Does NOT look like a time (e.g. "12:06", "3:00")
--      - Does NOT consist solely of game-status tokens
--      - Does NOT contain HTML entities (&)
--      - Does NOT contain position labels (ends with ,WR/. ,TE, etc.)
--      - Is NOT a bare day name or game status (Sat, Sun, Final, Half, etc.)
-- ---------------------------------------------------------------------
DELETE FROM league_members
WHERE fantasy_team IS NULL
   OR fantasy_team !~ '^\S+.*\S$'   -- empty or whitespace-only
   OR length(fantasy_team) < 3
   OR fantasy_team ~ '^[0-9]'          -- starts with a digit (Q1 12:06, L W, etc.)
   OR fantasy_team ~ '&'               -- HTML entities (@&nbsp;, vs&nbsp;)
   OR fantasy_team ~ '\b(Q[1-4][^,]*|Sat|Sun|Mon|Tue|Wed|Thu|Fri|am|pm|Final|Live|Half|Delay|1st|2nd|3rd|4th|Owned|W\s+L|L\s+W)\b'
   OR fantasy_team ~ '^\S+\s*[0-9]+:[0-9]+\s*[ap]m?$'  -- "Sat 3:00 pm"
   OR fantasy_team ~ ',(WR|TE|RB|QB|K|DEF)$'           -- position leak: "player,WR"
   OR fantasy_team ~ '^[WL]\s'                          -- "W vs" / "L W" records
   OR fantasy_team ~ '\bpm\b'                            -- bare "pm"
   OR fantasy_team ~ '\bam\b';                           -- bare "am"

-- ---------------------------------------------------------------------
-- 2. Delete orphaned leagues_members rows (no league_members reference)
-- ---------------------------------------------------------------------
DELETE FROM leagues_members lm
WHERE lm.platform = 'yahoo-cfb'
  AND NOT EXISTS (
      SELECT 1 FROM league_members lmem
      WHERE lmem.member_id = lm.id
  );

-- ---------------------------------------------------------------------
-- 3. Also update leagues_members.manager_name to match
-- ---------------------------------------------------------------------
DELETE FROM leagues_members
WHERE manager_name IS NULL
   OR length(manager_name) < 3
   OR manager_name ~ '^[0-9]'
   OR manager_name ~ '&'
   OR manager_name ~ '\b(Q[1-4][^,]*|Sat|Sun|Mon|Tue|Wed|Thu|Fri|am|pm|Final|Live|Half|Delay|1st|2nd|3rd|4th|Owned)\b'
   OR manager_name ~ ',(WR|TE|RB|QB|K|DEF)$'
   OR manager_name ~ '^[WL]\s'
   OR manager_name ~ '\bpm\b'
   OR manager_name ~ '\bam\b';

COMMIT;

-- =====================================================================
-- VERIFICATION
-- =====================================================================
-- SELECT fantasy_team, count(*) FROM league_members GROUP BY 1 ORDER BY 2 DESC;
-- SELECT count(*) FROM leagues_members;
-- SELECT count(*) FROM league_members;
-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- -- Re-run migration 022 backfill to restore (not recommended — re-run 022)
-- COMMIT;
