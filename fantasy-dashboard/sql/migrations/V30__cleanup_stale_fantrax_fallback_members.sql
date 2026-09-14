-- 030: Deduplicate leagues_members fallback rows
--
-- sync_fantrax_roster_assignments() inserts fallback leagues_members rows
-- (payload->>'source' = 'sync_fantrax_fallback') with ON CONFLICT ... DO NOTHING,
-- which leaves them alongside the real rows from upsert_fantrax_members()
-- (payload->>'source' = 'sync_fantrax'). This deletes the stale fallbacks
-- when a real row exists for the same (platform, league_id, fantasy_team).

BEGIN;

DELETE FROM leagues_members
WHERE payload->>'source' = 'sync_fantrax_fallback'
  AND EXISTS (
    SELECT 1 FROM leagues_members m2
    WHERE m2.platform = leagues_members.platform
      AND m2.league_id = leagues_members.league_id
      AND m2.fantasy_team = leagues_members.fantasy_team
      AND m2.payload->>'source' = 'sync_fantrax'
  );

COMMIT;
