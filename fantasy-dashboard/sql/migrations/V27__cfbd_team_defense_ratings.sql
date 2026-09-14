-- Migration 027: cfbd_team_defense_ratings
-- Team-level defensive performance data from CFBD /ratings endpoints.
-- Used to rank DEF roster entries (team defenses) on Yahoo NCAAF teams,
-- since CFBD has no individual athlete ID for team-level DEF slots.

-- cfbd_team_defense_ratings: one row per team per season, storing
-- defensive metrics from /ratings/sp (SP+), /ratings/fpi (FPI),
-- /ratings/core (advanced), and /ratings/srs (SRS).
-- The primary key is (team, season) so re-syncs overwrite the snapshot.

create table if not exists cfbd_team_defense_ratings (
    team            text    not null,
    season          integer not null,
    week            integer,           -- through_week / latest week available
    conference      text,               -- from /teams/fbs
    division        text,               -- from /teams/fbs

    -- SP+ (from /ratings/sp)
    sp_defense_ranking     integer,      -- SP+ defensive ranking (1 = best)
    sp_defense_rating      numeric(6,2), -- SP+ defensive rating
    sp_overall_ranking     integer,      -- overall SP+ ranking
    sp_overall_rating      numeric(6,2), -- overall SP+ rating

    -- FPI (from /ratings/fbi)
    fpi_defense     numeric(6,2),        -- FPI defensive rating
    fpi_overall     numeric(6,2),        -- overall FPI rating

    -- SRS (from /ratings/srs)
    srs_defense_ranking  integer,        -- SRS defensive ranking
    srs_defense_rating   numeric(6,2),   -- SRS defensive rating
    srs_overall_ranking  integer,        -- overall SRS ranking
    srs_overall_rating   numeric(6,2),   -- overall SRS rating

    -- Core ratings (from /ratings/core)
    core_defense          numeric(6,2),  -- defensive rating (points allowed per 100 plays above/below average)
    core_defense_ranking  integer,        -- defensive ranking from core ratings

    -- Deep SP+ defense metrics (from /ratings/sp defense sub-object)
    def_havoc            numeric(6,2),    -- SP+ defensive havoc rate
    def_passing_rating   numeric(6,2),    -- SP+ defensive passing rating
    def_rushing_rating   numeric(6,2),    -- SP+ defensive rushing rating
    def_explosiveness    numeric(6,2),    -- SP+ defensive explosiveness
    def_success_rate     numeric(6,2),    -- SP+ defensive success rate

    fetched_at  timestamptz not null default now()
);

-- Only add the PK constraint if it doesn't already exist.  The table
-- may have been created by a prior migration run that already added
-- the constraint, so a blind ADD CONSTRAINT would fail with
-- "multiple primary keys are not allowed".
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'cfbd_team_defense_ratings_pkey'
          and contype = 'p'
    ) then
        alter table cfbd_team_defense_ratings
            add constraint cfbd_team_defense_ratings_pkey
            primary key (team, season);
    end if;
end
$$;

create index if not exists idx_cfbd_ratings_season_rank
    on cfbd_team_defense_ratings (season, sp_defense_ranking);

create index if not exists idx_cfbd_ratings_conference
    on cfbd_team_defense_ratings (conference, season);
