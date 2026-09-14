-- =====================================================================
-- 017_player_week_stats.sql
--
-- Actual weekly production and usage. This is the foundation of the whole
-- projection model, and nothing like it existed.
--
-- Three things it enables that were impossible before:
--   1. Usage-trend detection — is this player's role growing or shrinking?
--   2. Projection backtesting — the Phase 3 acceptance gate is that our
--      projections beat a naive season-average baseline. Without actuals
--      there is no way to know whether the model is worse than nothing.
--   3. Floor/ceiling from real week-to-week variance, which the guillotine
--      survival objective requires. A mean cannot support "don't finish
--      last this week".
--
--   docker exec -i fantasy-db psql -U fantasy -d fantasy -v ON_ERROR_STOP=1 \
--     < sql/migrations/017_player_week_stats.sql
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

CREATE TABLE IF NOT EXISTS player_week_stats (
    id          bigserial PRIMARY KEY,
    player_id   bigint  NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    source_name text    NOT NULL,   -- nflverse | cfbd
    season      integer NOT NULL,
    week        integer NOT NULL,
    team_key     text,
    opponent_key text,

    -- Usage / opportunity. The volume half of the projection.
    snaps        integer,
    snap_share   numeric(5,4),
    targets      integer,
    target_share numeric(5,4),
    air_yards    numeric(8,2),
    carries      integer,
    carry_share  numeric(5,4),
    routes_run   integer,
    usage_rate   numeric(5,4),   -- CFBD player usage
    ppa_total    numeric(8,4),   -- CFBD PPA

    -- Raw production. The efficiency half.
    pass_att numeric(8,2), pass_cmp numeric(8,2),
    pass_yd  numeric(8,2), pass_td  numeric(6,2), interceptions numeric(6,2),
    rush_yd  numeric(8,2), rush_td  numeric(6,2),
    rec      numeric(6,2), rec_yd   numeric(8,2), rec_td numeric(6,2),
    fumbles_lost numeric(6,2), two_pt numeric(6,2), return_td numeric(6,2),

    -- Kicking and team defense, so K and DEF stop projecting to zero.
    fg_made_0_39 numeric(6,2), fg_made_40_49 numeric(6,2),
    fg_made_50_plus numeric(6,2), fg_missed numeric(6,2), xp_made numeric(6,2),
    def_sacks numeric(6,2), def_int numeric(6,2), def_fumbles_rec numeric(6,2),
    def_td numeric(6,2), def_safety numeric(6,2),
    def_points_allowed numeric(6,2), def_yards_allowed numeric(8,2),

    -- Pre-scored totals for the three common formats, so variance can be
    -- computed without re-deriving scoring every time.
    fantasy_points_std      numeric(8,2),
    fantasy_points_half_ppr numeric(8,2),
    fantasy_points_ppr      numeric(8,2),

    -- Did he actually play? Essential for variance: a DNP is not a 0-point
    -- performance, and averaging them in understates a player's real floor.
    played      boolean,
    is_bye      boolean NOT NULL DEFAULT false,

    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at  timestamptz NOT NULL DEFAULT now(),

    UNIQUE (player_id, source_name, season, week)
);

CREATE INDEX IF NOT EXISTS ix_pws_player_recent
    ON player_week_stats (player_id, season, week DESC);
CREATE INDEX IF NOT EXISTS ix_pws_season_week
    ON player_week_stats (season, week);

COMMIT;

-- =====================================================================
-- VERIFICATION (after sync_nflverse.py has run)
-- =====================================================================
-- SELECT source_name, season, week, count(*) AS rows,
--        count(snap_share) AS with_snaps,
--        count(fantasy_points_ppr) AS with_points
--   FROM player_week_stats GROUP BY 1,2,3 ORDER BY 1,2,3;
--
-- -- the variance basis for floor/ceiling; needs >= 3-4 weeks to mean much
-- SELECT p.player_name, p.pos, count(*) AS weeks,
--        round(avg(s.fantasy_points_ppr), 1) AS avg_pts,
--        round(stddev_samp(s.fantasy_points_ppr), 1) AS sd
--   FROM player_week_stats s JOIN players p ON p.id = s.player_id
--  WHERE s.played AND s.season = 2026
--  GROUP BY 1,2 HAVING count(*) >= 3
--  ORDER BY sd DESC NULLS LAST LIMIT 20;

-- =====================================================================
-- ROLLBACK
-- =====================================================================
-- BEGIN;
-- DROP TABLE IF EXISTS player_week_stats;
-- COMMIT;
