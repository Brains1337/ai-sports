-- Migration 028: Fix uix_players_platform_name_pos partial index
--
-- The partial index uix_players_platform_name_pos enforces uniqueness on
-- (platform, player_name, pos) WHERE (external_player_id IS NULL).
-- However, Fantrax players are inserted with external_player_key populated
-- but external_player_id left NULL. This means two Fantrax players from
-- different college teams with the same name and position (e.g., "Team
-- Offense" / "TO") collide on this index even though they have distinct
-- external_player_key values.
--
-- Fix: extend the partial index WHERE clause to also exclude rows that have
-- an external_player_key set, so the name+pos uniqueness check only applies
-- to truly "generic" placeholder players (ESPN-style with no external key
-- and no external_id).
BEGIN;

DROP INDEX IF EXISTS uix_players_platform_name_pos;

CREATE UNIQUE INDEX uix_players_platform_name_pos
ON public.players (platform, player_name, pos)
WHERE (external_player_id IS NULL AND external_player_key IS NULL);

-- Mirror in init.sql for dev DB parity
COMMENT ON INDEX uix_players_platform_name_pos IS
    'Type: INDEX; ensures unique (platform, player_name, pos) only for rows without any external key';

COMMIT;
