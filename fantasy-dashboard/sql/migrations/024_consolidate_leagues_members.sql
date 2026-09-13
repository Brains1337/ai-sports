-- Consolidate league_members into leagues_members
-- Eliminates the league_members join table by merging its columns directly
-- into leagues_members. Members can now be linked to a league in a single table.
--
-- Rationale: The two-table design (leagues_members + league_members) is
-- "extra waste" — it fragments member data across two tables requiring
-- an extra join on every query. Consolidating into leagues_members
-- simplifies inserts, reads, and maintenance.

begin;

-- 1. Add league-linking columns to leagues_members
ALTER TABLE leagues_members
    ADD COLUMN IF NOT EXISTS league_id bigint REFERENCES leagues(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS fantasy_team text,
    ADD COLUMN IF NOT EXISTS waiver_priority integer,
    ADD COLUMN IF NOT EXISTS team_slot integer,
    ADD COLUMN IF NOT EXISTS source_name text default 'yahoo';

-- 2. Backfill: copy league_members data into leagues_members
-- Match via (league_id, member_id) and update the member profiles
UPDATE leagues_members lm
SET league_id = lmsm.league_id,
    fantasy_team = lmsm.fantasy_team,
    waiver_priority = lmsm.waiver_priority,
    team_slot = lmsm.team_slot,
    payload = lm.payload || to_jsonb(lmsm)
FROM league_members lmsm
WHERE lm.id = lmsm.member_id
  AND lm.league_id IS NULL;

-- 3. Handle members that exist in leagues_members but aren't in league_members
-- (edge case: member profiles created without league linking)
-- These are platform-level profiles; the league_id stays NULL allowing
-- a single member to be linked to multiple leagues via duplicate rows.

-- 4. For a clean migration, insert league-linked rows where no matching
-- leagues_members row exists for that platform+external_member_key+league_id combo.
-- This uses INSERT ... SELECT with a NOT EXISTS guard.
INSERT INTO leagues_members
    (platform, external_member_key, manager_name, manager_email,
     payload, league_id, fantasy_team, waiver_priority, team_slot, source_name,
     created_at, updated_at)
SELECT
    lm_member.platform,
    lm_member.external_member_key,
    lm_member.manager_name,
    lm_member.manager_email,
    lm_member.payload || to_jsonb(lm_extra),
    lm.league_id,
    lm.fantasy_team,
    lm.waiver_priority,
    lm.team_slot,
    'yahoo' as source_name,
    coalesce(lm.created_at, lm_member.created_at),
    coalesce(lm.updated_at, lm_member.updated_at)
FROM league_members lm
JOIN leagues_members lm_member ON lm_member.id = lm.member_id
WHERE NOT EXISTS (
    SELECT 1 FROM leagues_members existing
    WHERE existing.platform = lm_member.platform
      AND existing.external_member_key = lm_member.external_member_key
      AND existing.league_id = lm.league_id
);

-- 5. Add composite unique index: each manager has one fantasy_team per league
create unique index if not exists ux_leagues_members_platform_key_league
    on leagues_members (platform, external_member_key, league_id);

-- 6. Add index on league_id for fast roster_assignments joins
create index if not exists ix_leagues_members_league
    on leagues_members (league_id);

-- 7. Drop redundant league_members table
DROP TABLE IF EXISTS league_members CASCADE;

-- 8. Drop now-orphaned sequence
DROP SEQUENCE IF EXISTS league_members_id_seq CASCADE;

commit;
