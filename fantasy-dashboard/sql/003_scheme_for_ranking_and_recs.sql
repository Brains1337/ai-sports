-- 1) Extend leagues with waiver metadata
alter table leagues
  add column if not exists waiver_type text,             -- 'FAAB', 'priority', 'continuous'
  add column if not exists waiver_clear_time timestamptz,
  add column if not exists add_drop_lock_window int,     -- minutes before kickoff
  add column if not exists in_week_adds_allowed boolean default true;

-- 2) Rankings (per week/sport/scoring_type)
create table if not exists rankings (
  id bigserial primary key,
  sport text not null,               -- 'NFL', 'NCAAF'
  scoring_type text not null,        -- 'PPR', 'STD', etc.
  week int not null,
  player_id bigint not null references players(id) on delete cascade,
  proj_pts numeric not null,
  opp_team text,
  def_strength numeric,              -- lower = tougher D, or normalized z-score
  composite_score numeric not null,
  created_at timestamptz not null default now()
);

create index if not exists rankings_week_sport_scoring_idx
  on rankings (sport, scoring_type, week);

-- 3) Start/sit recs
create table if not exists start_sit_recommendations (
  id bigserial primary key,
  league_id bigint not null references leagues(id) on delete cascade,
  week int not null,
  slot text not null,                -- 'QB1','RB1','RB2','WR1','FLEX', etc.
  player_id bigint not null references players(id) on delete cascade,
  composite_score numeric not null,
  recommended_action text not null,  -- 'start' or 'bench'
  rationale text,
  created_at timestamptz not null default now()
);

create index if not exists ssr_league_week_idx
  on start_sit_recommendations (league_id, week);

-- 4) Waiver targets
create table if not exists waiver_targets (
  id bigserial primary key,
  league_id bigint not null references leagues(id) on delete cascade,
  week int not null,
  player_id bigint not null references players(id) on delete cascade,
  projected_pts numeric not null,
  priority_score numeric not null,   -- higher = better target
  recommended_drop_player_id bigint references players(id),
  status text not null default 'open', -- 'open','queued','executed','dismissed'
  rationale text,
  created_at timestamptz not null default now()
);

create index if not exists waiver_targets_league_week_idx
  on waiver_targets (league_id, week);

-- 5) Drop candidates
create table if not exists drop_candidates (
  id bigserial primary key,
  league_id bigint not null references leagues(id) on delete cascade,
  week int not null,
  player_id bigint not null references players(id) on delete cascade,
  composite_score numeric not null,
  replacement_delta numeric not null, -- how far below FA replacement
  reason_code text not null,          -- 'below_replacement','lost_role','injured', etc.
  rationale text,
  created_at timestamptz not null default now()
);

create index if not exists drop_candidates_league_week_idx
  on drop_candidates (league_id, week);