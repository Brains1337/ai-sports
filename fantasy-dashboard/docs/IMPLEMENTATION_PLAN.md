# AI Sports Platform — Implementation Plan

> **Status:** Revised against live preflight output — 2026-09-08
> **Preflight verdict:** rename collisions clear, migration `010` safe to apply. But see §1.7 — the headline blocker is *not* the platform literal.
> **Scope:** Everything under `fantasy-dashboard/`. `sql/init.sql` is treated as read-only truth about the live database.
> **Goal:** A correct, weekly-granular data pipeline feeding a local LLM (24GB Mac Mini) that decides start / sit / drop / pickup for four leagues.

---

## 1. Diagnosis: why Yahoo data isn't landing

The Yahoo problem is real but it is not the whole problem. The audit found a single dominant failure mode across the entire pipeline:

**Every failure is silent.** Nothing throws. Scripts print success counts, rows get written, and then downstream views and planners return zero rows because a string literal doesn't match, a column is NULL, or an exception handler catches the wrong class. The platform looks healthy and produces nothing.

There are four independent root causes.

### 1.1 `platform` literal fork

`sync_espn.py` and `sync_espn_rosters.py` write `platform = 'espn'`. The `league_rosters` and `waiver_wire` views, and `waiver_planner.py`, all expect `'espn-nfl'`:

```sql
-- init.sql:281-288 — no branch matches a stored platform of 'espn'
LEFT JOIN projections pr ON pr.player_id = rsl.player_id AND pr.source_name =
    CASE l.platform
        WHEN 'yahoo-cfb'   THEN 'cfbd_cfb_proj_yahoo'
        WHEN 'fantrax-cfb' THEN 'cfbd_cfb_proj_fantrax'
        WHEN 'espn-nfl'    THEN 'fantasypros_proj'
        WHEN 'yahoo-nfl'   THEN 'fantasypros_proj'
        ELSE NULL
    END
```

Consequence: `projected_points` is **always NULL for the two ESPN leagues**. `build_rankings.py` filters `leagues.platform = 'espn'` while `waiver_planner.py` keys on `'espn-nfl'`, so the two disagree about which rows exist and one of them always loads nothing.

(The `yahoo-cfb` and `fantrax-cfb` branches do match — assuming those are the literals actually stored, which the §5.0 preflight confirms.)

### 1.2 `sport` is missing for Fantrax

**Corrected after preflight.** My original claim — that ESPN never writes `sport` — was wrong. ESPN players all carry `sport='NFL'` (11,616 of 11,616). The real gap is Fantrax:

| platform | rows | sport set |
|---|---|---|
| `espn` | 11,616 | all `NFL` |
| `yahoo-cfb` | 2,077 | all `NCAAF` |
| `fantrax-cfb` | 16,431 | **997 only** — 15,434 NULL |

Consequence, confirmed by the preflight: `sync_cfbd_player_xref.py` and `sync_cfbd_cfb_projections.py` both filter `sport='NCAAF'`, and **zero `fantrax-cfb` players carry a `cfbd_athlete_id`** (versus 1,954 of 2,077 for Yahoo — a 94% match rate). There is no `cfbd_cfb_proj_fantrax` source in `projections` at all. The Fantrax league has never had a single projection.

### 1.3 No week dimension

`projections` has no `week` column. Combined with:

- `sync_fantasypros.py` never sending FantasyPros' optional `week` param (`week=0` is their season-long sentinel), so only season totals ever arrive;
- `sync_cfbd_cfb_projections.py` deleting by `(source_name, season)` on every run, so each weekly run **destroys the previous week's rows**;

…weekly start/sit is not actually computable on the current schema. `build_rankings.py` writes a `week` label onto season-total numbers, which is worse than having no week at all because it looks correct.

### 1.4 `sync_cfbd_cfb_projections.py` is not projecting anything

It calls CFBD `/games/players`, which returns **completed-game actuals**. Week 1's "projection" is literally last week's box score. Before games are played the response is empty, 0 rows are written, and the script `sys.exit(1)`s.

### 1.5 Yahoo specifically

`sync_yahoo.py` regex-parses rendered HTML. The symptoms in the code tell the story — there is a whole defensive layer (`_INVALID_TEAM_RE`, `is_valid_team_name`, three separate normalize-and-revalidate passes) built to filter scraper artifacts like `"W (Sep 9)"`, `"FA"`, and bare numeric stat rows that leak into `fantasy_team`. That layer exists because `parse_roster_status()` guesses ownership from a text regex:

```python
m = re.search(r"\bTeam\s+([A-Za-z0-9 .'\-]{2,30})", row_text)
```

Anything that doesn't match returns `"unknown"`. Those rows do survive into `roster_status_latest` (its `CASE` has an `ELSE 3` branch), but both downstream views filter them out — `league_rosters` requires `roster_status='owned'` and `waiver_wire` requires `free_agent`/`waivers`. So an unknown-status player is stored, counted as a success, and is invisible to every consumer. `sync_yahoo.py` also writes `external_player_id = NULL` for every player, forcing name-based matching everywhere downstream.

**Confirmed constraint:** Yahoo's public Fantasy Sports API has no `cfb` game code — only NFL, NBA, MLB, NHL. The official API is not an option for the Yahoo EDIT League. Session-cookie access to Yahoo's own JSON is the only viable path.

### 1.6 The actual headline blocker: FantasyPros has 47 players

This only became visible with live data, and it is worse than everything above.

```
source_name       rows  distinct_players  fetched
fantasypros_proj    47                47  2026-08-30   (9 days stale)
fantasypros_ecr     47                47  2026-08-30
cfbd_cfb_proj_yahoo 823              823  2026-09-08
cfbd_cfb_proj_fantrax  — does not exist —
```

**47 NFL players.** The two ESPN leagues have 415 owned players between them. Even with the platform literal fixed, projection coverage cannot exceed ~11% of rostered players. I said in my first pass that migration `010` "alone should make ESPN projections appear in the dashboard" — that was overstated, and I want to correct it clearly: `010` unblocks the join, but there is almost nothing on the other side of it to join to.

The likely cause is visible in the same output. `player_xref` holds **94** FantasyPros source keys mapping onto **47** distinct `players.id`. That is a 2:1 collapse — multiple different FantasyPros players resolving to the same database player. This is the "always returns a match" bug in `find_best_player_match` (§1.6): `best_score` starts at −1, so any candidate row scores higher and wins. Names that can't match exactly fall through to a first-name `LIKE`, which then binds several real players onto one row.

Also note `player_xref.confidence` is stored on a **0–100 scale** (avg 88.085), not the 0.0–1.0 the schema comment implies. The new matcher must pick one scale and normalize existing rows.

**Priority consequence:** fixing `sync_fantasypros.py` (week param + all scoring formats + a matcher that returns `None` instead of guessing) moves from Phase 3 to **Phase 1**. It is the difference between the ESPN leagues having usable projections and not.

### 1.7 ESPN has no free agent pool at all

`waiver_wire` returns rows for only two leagues:

```
Fantrax New Freshman   16,281 available    0 with projections
Yahoo EDIT League         217 available   30 with projections
The_Dark_Side              — absent —
TX Guillotine league       — absent —
```

`sync_espn_rosters.py` hardcodes `roster_status='owned'` and only walks team rosters, so it never records a free agent. In a **guillotine league, the waiver pool is the entire game** — every week an eliminated team's whole roster becomes available. The two leagues where waiver intelligence matters most currently have zero waiver data.

Meanwhile Fantrax has 16,281 "available" players because it ingests the entire global directory rather than the league's custom 73-team pool. Both are useless for different reasons.

### 1.8 Other things the live data exposed

- **`roster_status='owned'` with `fantasy_team IS NULL`: 5,382 rows.** We know a player is owned but not by whom. That breaks `is_my_team` and all opponent tracking. Needs a sync fix, not a data fix — `010` deliberately leaves these alone.
- **No `'waivers'` status exists anywhere.** Only `owned`, `bench`, `free_agent`, `unknown`. Yahoo's waiver period is never detected, so the `waiver_wire` view's `waivers` branch is dead.
- **TX Guillotine reports 11 distinct `fantasy_team` values in a 10-team league** — one artifact team name is in the data, exactly the class of garbage the Yahoo regex layer was built to filter. **Yahoo reports 12 distinct teams in a 16-team league** — four teams' rosters are simply missing.
- **ESPN `pos` is `DEF`, not `D/ST`** (32 rows), while `league_slots` uses `D/ST` and FantasyPros is queried for `DST`. Three different literals for one position. There are also 419 ESPN players with `pos='UNKNOWN'` as a literal string.
- **Fantrax `pos` is badly polluted:** 2,615 `OL`, plus `LS`, `LOG`, `TK`, `Default`, `Tm`, `TmD`, `ST`, and 143 empty strings. Only 11 rows have `pos='DEF'` despite the league needing ~73 team defenses. Notably `TO` (Team Offense) and `TQB` (Team QB) do appear — so Fantrax does expose team-aggregate positions.
- **`rankings` confirms the duplicate bug:** NFL week 1 has 94 rows for 47 distinct players — exactly 2×, one per ESPN league.
- **`sources` shows only `espn` and `fantasypros` ever logged a fetch.** Yahoo, Fantrax, and CFBD write no audit rows at all, which is why the staleness was invisible.
- `draft_rules` is empty, as expected.

### 1.9 Other bugs found by code review

| Bug | File | Effect |
|---|---|---|
| League resolved by `where platform = :platform order by id limit 1` | `sync_fantrax.py` | Multi-league runs write all history to the first Fantrax league |
| Never creates its own `leagues` row | `sync_fantrax.py` | Requires undocumented manual bootstrap |
| Base-36 mangling of alphanumeric IDs into `bigint` | `sync_fantrax.py` | Lossy; `ValueError` → NULL id; overflow risk |
| `from psycopg import ProgrammingError` | yahoo, fantrax | SQLAlchemy raises `sqlalchemy.exc.*` — handler never fires, one bad row aborts the whole transaction |
| `normalize_name()` strips punctuation, SQL compares unstripped | `sync_fantasypros.py` | `A.J. Brown`, `D'Andre Swift`, `Amon-Ra St. Brown` never match; first-name `LIKE` fallback **always** returns something (`best_score` starts at -1) → wrong-player xrefs written |
| `on conflict (platform, external_player_id)` doesn't cover the partial unique index | espn, fantrax | IntegrityError on second same-name/pos NULL-id player |
| Player fetch capped at `limit=2000`, no pagination | `sync_espn.py` | Silent truncation |
| `pos`/`slot` maps disagree on id 16 (`DEF` vs `D/ST`) | `sync_espn.py` | FantasyPros requests `DST`, DB stores `D/ST`, bonus never fires |
| Duplicate `rankings` rows | `build_rankings.py` | Deletes once per (sport, scoring_type, week), then every league re-inserts the full pool under the same key |
| CFB rankings delete inside the per-league loop | `build_rankings.py` | Second CFB league wipes the first's rows if scoring types collide |
| `latest_proj` CTE has no `season` filter | `build_rankings.py` | A stale-season row with newer `fetched_at` wins |
| Writer uses `CFBD_SEASON`, reader uses `ESPN_SEASON` | cfbd / build_rankings | Silent empty pool when they diverge |
| `_PLATFORM_SOURCE_PREFIX` accepted and ignored | `waiver_planner.py` | Dead code; `rankings` has no `source_name`, so two CFB leagues with equal scoring types get each other's players as free agents |
| Skips any league with `scoring_type IS NULL` | `waiver_planner.py` | Rankings get written under a default, then never read |
| `slot = f"SLOT-{idx}"` | `waiver_planner.py` | Positional index, not a real lineup slot name |
| `cfbd_athlete_id is null` filter | `sync_cfbd_player_xref.py` | A wrong mapping is never re-evaluated; must be manually nulled |
| Doesn't populate `player_xref` despite the name | `sync_cfbd_player_xref.py` | No auditable CFB mapping table |

---

## 2. Strategy: the thing the current design is missing

Both ESPN leagues are **guillotine** format. Nothing in the codebase models this, and it changes the optimization target completely:

- Each week the lowest-scoring team is eliminated and **their entire roster hits the free agent pool**. The waiver pool gets a large, high-quality influx on a known schedule.
- The objective is not "maximize expected points," it is **"do not finish last this week."** That means maximizing the *floor* of your lineup, not the ceiling — the opposite of tournament DFS logic. A boom/bust WR with a 22-point ceiling and a 2-point floor is actively dangerous.
- Late in the season, as the field shrinks, the objective flips toward ceiling because you need to beat fewer, stronger teams.

A correct engine for these two leagues needs projected **distributions**, not point estimates, and a survival-probability objective that shifts weight from floor to ceiling as `teams_remaining` shrinks. This is called out in §7 and is a deliberate addition to scope, because a start/sit engine that optimizes mean points will give measurably wrong advice in a guillotine league.

Open question for you in §11: confirm both ESPN leagues are guillotine, and whether elimination is by weekly score or cumulative.

---

## 3. Target architecture

```
                    ┌─────────────────────────────────────────┐
                    │  scripts/common/  (NEW)                 │
                    │  constants.py · db.py · matching.py     │
                    │  http.py · logging.py                   │
                    └─────────────────────────────────────────┘
                                     │ imported by every script
   ─────────────────────────────────────────────────────────────────
   LEAGUE STATE (who owns whom)          PLAYER TRUTH (how good are they)

   sync_espn.py         ──┐              sync_fantasypros.py  ──┐
   sync_espn_rosters.py ──┤              sync_cfbd_reference.py ─┤
   sync_yahoo.py  (JSON)──┼──→ leagues   sync_cfbd_usage.py    ──┼──→ projections
   sync_fantrax.py      ──┤    players   sync_cfbd_lines.py    ──┤    (now week-aware)
                          │    roster_   sync_player_news.py   ──┘    player_week_stats
                          │    status_                              matchup_schedule
                          │    history                              player_events
                          └────────────┬──────────────────────────────┘
                                       ▼
                          build_projections.py   (NEW — real CFB model)
                          build_rankings.py      (rewritten)
                                       ▼
                          recommend.py           (NEW — deterministic engine)
                          → start_sit_recommendations
                            waiver_targets · drop_candidates
                                       ▼
                          mcp_server.py          (NEW)
                                       ▼
                          Local LLM on Mac Mini
```

Two principles:

**Separate league state from player truth.** Ownership data comes from the fantasy platforms and is per-league. Player quality data comes from FantasyPros/CFBD and is global. Mixing them is why `rankings` has cross-league contamination today.

**One constants module, zero inline literals.** Every `'espn-nfl'`, `'NCAAF'`, `'fantasypros_proj'` moves to `scripts/common/constants.py` with enums. This class of bug cannot recur if the strings exist in exactly one place.

---

## 4. Canonical vocabulary

Locking these down now, chosen to minimize churn against what the code already does:

| Concept | Canonical values | Notes |
|---|---|---|
| `platform` | `espn-nfl`, `yahoo-nfl`, `yahoo-cfb`, `fantrax-cfb`, `fantrax-nfl` | ESPN migrates `espn` → `espn-nfl` |
| `sport` | `NFL`, `NCAAF` | Uppercase — matches existing CFBD filters. Enforced by CHECK |
| `scoring_type` | `STD`, `HALF_PPR`, `PPR` | Uppercase. Enforced by CHECK |
| `roster_status` | `owned`, `waivers`, `free_agent` | Lowercase. `unknown` is banned — see §5.1 |
| `lineup_status` | `starter`, `bench`, `ir`, `taxi` | **New column** — currently conflated into `roster_status` |
| `week` | `0` = season-long, `1..15` = game week | `0` matches FantasyPros' sentinel |

`roster_status` and `lineup_status` being one field today is why Fantrax's IR/taxi/minors all collapse to `bench`, and why `sync_espn_rosters.py` hardcodes `"owned"` and throws away `lineupSlotId`.

---

## 5. Schema migrations

Numbered, idempotent, transactional, each with a rollback block. Apply in order. **You run these** — nothing in this plan executes DDL against the live database.

Files land in `fantasy-dashboard/sql/migrations/`.

### 5.0 `009_preflight.sql` — run and read this FIRST

`init.sql` is a **schema-only** dump. It tells us nothing about what values are actually stored, and migration `010` is a data rewrite. Three of its statements can abort or silently corrupt depending on what's in there. Run these read-only queries and paste me the output before applying anything:

```sql
-- What platform literals actually exist? 010 only handles 'espn'.
SELECT 'players' src, platform, count(*) FROM players GROUP BY 1,2
UNION ALL SELECT 'leagues', platform, count(*) FROM leagues GROUP BY 1,2
UNION ALL SELECT 'pro_teams', platform, count(*) FROM pro_teams GROUP BY 1,2
ORDER BY 1,2;

-- Would the espn -> espn-nfl rename violate a unique constraint?
-- Both must return ZERO rows.
SELECT external_player_id FROM players
 WHERE platform IN ('espn','espn-nfl') AND external_player_id IS NOT NULL
 GROUP BY 1 HAVING count(DISTINCT platform) > 1;

SELECT player_name, pos FROM players
 WHERE platform IN ('espn','espn-nfl') AND external_player_id IS NULL
 GROUP BY 1,2 HAVING count(*) > 1;

SELECT external_team_id, season FROM pro_teams
 WHERE platform IN ('espn','espn-nfl')
 GROUP BY 1,2 HAVING count(DISTINCT platform) > 1;

-- Existing sport / scoring_type / roster_status vocabularies
SELECT DISTINCT sport FROM players;
SELECT DISTINCT sport, scoring_type FROM leagues;
SELECT roster_status, (fantasy_team IS NULL) AS team_is_null, count(*)
  FROM roster_status_history GROUP BY 1,2 ORDER BY 1,2;

-- Actual table sizes (the "27 MB" figure came from your briefing doc, not the dump)
SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid))
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relkind='r' ORDER BY pg_total_relation_size(c.oid) DESC;
```

Two things this decides. First, whether any rows already use `'espn-nfl'` — if so the rename in `010` collides with `players_platform_external_player_id_key` and `pro_teams_platform_external_team_id_season_key` and aborts the transaction, and those rows have to be merged first. Second, whether Yahoo/Fantrax rows are stored as bare `'yahoo'`/`'fantrax'` rather than the suffixed canonical values. If they are, `010` needs explicit UPDATEs for them too — and `'yahoo'` is ambiguous (maps to both `yahoo-nfl` and `yahoo-cfb`), so it has to be disambiguated per league.

### 5.1 `010_canonical_literals.sql`

Backfills the value mismatches. This is the single highest-value migration — it alone should make ESPN projections appear in the dashboard.

```sql
BEGIN;

-- Platform rename. Extend this list from the 009 preflight output before running.
UPDATE players   SET platform = 'espn-nfl' WHERE platform = 'espn';
UPDATE leagues   SET platform = 'espn-nfl' WHERE platform = 'espn';
UPDATE pro_teams SET platform = 'espn-nfl' WHERE platform = 'espn';

-- pro_teams is the only one of the three with a DEFAULT; without this, the next
-- insert that omits platform silently reintroduces 'espn'.
ALTER TABLE pro_teams ALTER COLUMN platform DROP DEFAULT;

-- sport backfill. Normalize case FIRST, then fill only what's still unset.
-- ELSE sport is load-bearing: without it, any platform lacking a -nfl/-cfb
-- suffix gets its sport nulled out.
UPDATE players SET sport = upper(sport) WHERE sport IS NOT NULL;
UPDATE leagues SET sport = upper(sport) WHERE sport IS NOT NULL;
UPDATE players SET sport = 'NCAAF' WHERE sport IN ('CFB','NCAA','COLLEGE');
UPDATE leagues SET sport = 'NCAAF' WHERE sport IN ('CFB','NCAA','COLLEGE');

UPDATE players SET sport = CASE
    WHEN platform LIKE '%-nfl' THEN 'NFL'
    WHEN platform LIKE '%-cfb' THEN 'NCAAF'
    ELSE sport
END
WHERE sport IS NULL;

UPDATE leagues SET sport = CASE
    WHEN platform LIKE '%-nfl' THEN 'NFL'
    WHEN platform LIKE '%-cfb' THEN 'NCAAF'
    ELSE sport
END
WHERE sport IS NULL;

-- scoring_type normalization
UPDATE leagues SET scoring_type = upper(replace(trim(scoring_type), ' ', '_'))
WHERE scoring_type IS NOT NULL;
UPDATE leagues SET scoring_type = 'HALF_PPR'
WHERE scoring_type IN ('HALF','0.5_PPR','HALFPPR','HALF-PPR');
UPDATE leagues SET scoring_type = 'STD'
WHERE scoring_type IN ('STANDARD','NONE','NON-PPR','NON_PPR');

-- my_team_name: promote payload value into the real column
UPDATE leagues
SET my_team_name = payload->>'my_team_name'
WHERE my_team_name IS NULL AND payload->>'my_team_name' IS NOT NULL;

-- Resolve 'unknown' roster statuses in BOTH directions, then ban the value.
UPDATE roster_status_history SET roster_status = 'free_agent'
WHERE roster_status = 'unknown' AND fantasy_team IS NULL;
UPDATE roster_status_history SET roster_status = 'owned'
WHERE roster_status = 'unknown' AND fantasy_team IS NOT NULL;

ALTER TABLE roster_status_history ADD CONSTRAINT rsh_roster_status_chk
    CHECK (roster_status IN ('owned','waivers','free_agent')) NOT VALID;

-- sport is nullable, and NULL IN (...) evaluates to NULL, which PASSES a CHECK.
-- The IS NOT NULL clause is what actually enforces the backfill.
ALTER TABLE players ADD CONSTRAINT players_sport_chk
    CHECK (sport IS NOT NULL AND sport IN ('NFL','NCAAF')) NOT VALID;
ALTER TABLE leagues ADD CONSTRAINT leagues_sport_chk
    CHECK (sport IS NOT NULL AND sport IN ('NFL','NCAAF')) NOT VALID;
ALTER TABLE leagues ADD CONSTRAINT leagues_scoring_chk
    CHECK (scoring_type IS NULL OR scoring_type IN ('STD','HALF_PPR','PPR')) NOT VALID;

COMMIT;
```

`NOT VALID` means the constraint applies to new writes without scanning and failing on legacy rows. Run `VALIDATE CONSTRAINT` only after the verification queries below all return clean — `VALIDATE` scans the whole table and will reject any row the backfill missed.

**Verify before committing (all three must be empty):**

```sql
SELECT platform, sport, count(*) FROM players  GROUP BY 1,2 HAVING sport IS NULL;
SELECT id, platform, sport, scoring_type, my_team_name FROM leagues WHERE sport IS NULL;
SELECT DISTINCT scoring_type FROM leagues
 WHERE scoring_type IS NOT NULL AND scoring_type NOT IN ('STD','HALF_PPR','PPR');

-- the payoff: projected_points should now be non-null for ESPN
SELECT league_name, count(*) AS players, count(projected_points) AS with_proj
  FROM league_rosters GROUP BY 1;
```

### 5.2 `011_projections_week.sql`

Adds the week dimension and makes projections idempotent. Currently every run appends unbounded duplicate rows.

```sql
BEGIN;

ALTER TABLE projections ADD COLUMN IF NOT EXISTS week integer NOT NULL DEFAULT 0;
ALTER TABLE projections ADD COLUMN IF NOT EXISTS floor_points  numeric(10,2);
ALTER TABLE projections ADD COLUMN IF NOT EXISTS ceiling_points numeric(10,2);
ALTER TABLE projections ADD COLUMN IF NOT EXISTS std_dev       numeric(10,2);
ALTER TABLE projections ADD COLUMN IF NOT EXISTS opportunity   numeric(10,2); -- touches/targets
ALTER TABLE projections ADD COLUMN IF NOT EXISTS confidence    numeric(4,3);
-- stop encoding ECR into news_summary as text (see 6.5)
ALTER TABLE projections ADD COLUMN IF NOT EXISTS ecr_rank integer;
ALTER TABLE projections ADD COLUMN IF NOT EXISTS ecr_tier integer;

-- Collapse existing duplicates, keeping the freshest row per key.
-- NOTE: `week` was just added with DEFAULT 0, so every pre-existing row is
-- week=0 here and all historical duplicates collapse into the season bucket.
-- That is intended — none of the existing rows were ever week-specific.
DELETE FROM projections p USING projections q
WHERE p.player_id = q.player_id
  AND p.source_name = q.source_name
  AND p.season = q.season
  AND p.week = q.week
  AND coalesce(p.scoring_format,'') = coalesce(q.scoring_format,'')
  AND (p.fetched_at < q.fetched_at OR (p.fetched_at = q.fetched_at AND p.id < q.id));

CREATE UNIQUE INDEX IF NOT EXISTS ux_projections_key
    ON projections (player_id, source_name, season, week, coalesce(scoring_format,''));

COMMIT;
```

`coalesce(scoring_format,'')` is used rather than the bare column because `scoring_format` is nullable and NULLs are distinct by default, which would let duplicate NULL-format rows slip past the key. A bare `COALESCE` is legal as an index element in PG16 and is IMMUTABLE-safe.

No separate lookup index — `(player_id, source_name, season, week)` is a strict leading prefix of `ux_projections_key`, so the unique index already serves those queries.

The unique index is what lets every projection sync become an upsert instead of a blind append. Because the key includes an expression, the conflict target must be spelled out in full:

```sql
INSERT INTO projections (...) VALUES (...)
ON CONFLICT (player_id, source_name, season, week, coalesce(scoring_format,''))
DO UPDATE SET projected_points = excluded.projected_points, ...
```

`ON CONFLICT ux_projections_key` is **not** valid syntax — `ON CONSTRAINT` accepts constraint names, not index names, and this is an index.

`floor_points`/`ceiling_points`/`std_dev` are what the guillotine survival model in §7 needs.

### 5.3 `012_leagues_identity.sql`

Two problems. First, `UNIQUE(external_league_id)` collides across seasons and platforms. Second — and this one is a hard blocker discovered from the league settings — **`external_league_id` is `bigint`, but the Fantrax league ID is `2hbybmp6msnsbuqa`.** That league literally cannot be stored in the current schema.

This is the actual root cause of the `sync_fantrax.py` bug in §1.6. It resolves its league with `order by id limit 1` not out of laziness but because *there is no way to look it up by external ID*. Fixing the lookup without fixing the column would just move the failure.

```sql
BEGIN;

-- Text-native external id for platforms with alphanumeric league keys.
ALTER TABLE leagues ADD COLUMN IF NOT EXISTS external_league_key text;

-- Backfill from the numeric column so both are populated going forward.
UPDATE leagues SET external_league_key = external_league_id::text
 WHERE external_league_key IS NULL AND external_league_id IS NOT NULL;

-- external_league_id becomes optional; external_league_key is canonical.
ALTER TABLE leagues ALTER COLUMN external_league_id DROP NOT NULL;

ALTER TABLE leagues DROP CONSTRAINT IF EXISTS leagues_external_league_id_key;
ALTER TABLE leagues ADD  CONSTRAINT leagues_platform_extkey_season_key
    UNIQUE (platform, external_league_key, season);

COMMIT;
```

All league lookups move to `(platform, external_league_key, season)`. `external_league_id` is retained only for ESPN/Yahoo convenience and should be treated as derived.

The same fix applies to `players.external_player_id` for Fantrax player ids — that's `external_player_key`, added in `020`.

### 5.4 `013_lineup_status.sql`

Splits ownership from lineup role.

```sql
BEGIN;
ALTER TABLE roster_status_history
    ADD COLUMN IF NOT EXISTS lineup_status text,
    ADD COLUMN IF NOT EXISTS slot_name     text;

-- Order matters: tag lineup_status BEFORE overwriting roster_status.
-- Idempotent on re-run — after pass 1 no roster_status='bench' rows remain.
UPDATE roster_status_history SET lineup_status = 'bench'
WHERE lineup_status IS NULL AND roster_status = 'bench';
UPDATE roster_status_history SET roster_status = 'owned'
WHERE roster_status = 'bench';

-- Index must match roster_status_latest's actual ORDER BY, which puts a CASE
-- expression between the keys and fetched_at. A plain
-- (league_id, player_id, fetched_at) index cannot supply this sort.
CREATE INDEX IF NOT EXISTS ix_rsh_latest_sort
    ON roster_status_history (
        player_id, league_id,
        (CASE roster_status WHEN 'owned' THEN 0 WHEN 'waivers' THEN 1
                            WHEN 'free_agent' THEN 2 ELSE 3 END),
        fetched_at DESC
    );
COMMIT;
```

**Ship `013` and `018` in the same deploy.** `013` reclassifies every bench player from `roster_status='bench'` to `'owned'`, and `league_rosters` filters on `roster_status='owned'`. Between the two migrations, bench players appear in the roster view with no way to distinguish them from starters. `018` is what exposes `lineup_status` so they can be told apart.

### 5.5 `014_matchup_schedule.sql`

The table `build_rankings.py` needs so `opp_team` and `def_strength` stop being hardcoded NULL.

```sql
BEGIN;
CREATE TABLE IF NOT EXISTS matchup_schedule (
    id              bigserial PRIMARY KEY,
    sport           text    NOT NULL,
    season          integer NOT NULL,
    week            integer NOT NULL,
    team_key        text    NOT NULL,   -- normalized team identity
    opponent_key    text,
    is_home         boolean,
    is_bye          boolean NOT NULL DEFAULT false,
    kickoff_at      timestamptz,
    vegas_spread    numeric(6,2),       -- negative = favored
    vegas_total     numeric(6,2),
    implied_points  numeric(6,2),       -- (total/2) - (spread/2)
    opp_def_rank_overall numeric(6,2),
    opp_def_ppa_pass numeric(8,4),
    opp_def_ppa_rush numeric(8,4),
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sport, season, week, team_key)
);
COMMIT;
```

(No extra lookup index — the `UNIQUE` constraint's implicit index already covers `(sport, season, week, team_key)` exactly.)

`implied_points` is the single most predictive team-level input for fantasy scoring and is currently absent from the platform entirely.

### 5.6 `015_player_events.sql`

Injury / news / depth-chart events. Required for the alerting gap and for the model to know a player is questionable.

```sql
BEGIN;
CREATE TABLE IF NOT EXISTS player_events (
    id           bigserial PRIMARY KEY,
    player_id    bigint REFERENCES players(id) ON DELETE CASCADE,
    event_type   text NOT NULL,   -- injury | news | depth_chart | practice | transaction
    severity     text,            -- out | doubtful | questionable | probable | info
    headline     text,
    body         text,
    source_name  text NOT NULL,
    source_url   text,
    event_at     timestamptz NOT NULL,
    content_hash text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- NULLS NOT DISTINCT is essential: player_id is nullable so news about an
    -- unmatched player can still be stored (see 6.1's no-force-match rule).
    -- With default NULL-distinct semantics those rows would re-insert on every
    -- poll and re-fire Telegram alerts — the exact bug this key exists to stop.
    UNIQUE NULLS NOT DISTINCT (player_id, source_name, content_hash)
);
CREATE INDEX IF NOT EXISTS ix_player_events_recent
    ON player_events (player_id, event_at DESC);

ALTER TABLE players ADD COLUMN IF NOT EXISTS injury_status text;
ALTER TABLE players ADD COLUMN IF NOT EXISTS injury_updated_at timestamptz;
COMMIT;
```

The `content_hash` unique key is what makes the news poller idempotent and prevents duplicate Telegram alerts without a separate dedupe table. Note `players.injury_status` does not currently exist — only `projections.injury_status` does — so this `ADD COLUMN` is a genuine addition, not a duplicate.

### 5.7 `016_player_week_stats.sql`

Actual weekly production. Needed for three things the platform can't do today: usage-trend detection, projection backtesting, and "is this player's role actually growing."

```sql
BEGIN;
CREATE TABLE IF NOT EXISTS player_week_stats (
    id            bigserial PRIMARY KEY,
    player_id     bigint NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    source_name   text NOT NULL,
    season        integer NOT NULL,
    week          integer NOT NULL,
    team_key      text,
    opponent_key  text,
    snaps         integer,
    snap_share    numeric(5,4),
    targets       integer,
    target_share  numeric(5,4),
    carries       integer,
    usage_rate    numeric(5,4),   -- CFBD player usage
    ppa_total     numeric(8,4),   -- CFBD PPA
    pass_yd numeric(8,2), pass_td numeric(6,2), interceptions numeric(6,2),
    rush_yd numeric(8,2), rush_td numeric(6,2),
    rec     numeric(6,2), rec_yd  numeric(8,2), rec_td numeric(6,2),
    fumbles_lost numeric(6,2),
    fantasy_points_std      numeric(8,2),
    fantasy_points_half_ppr numeric(8,2),
    fantasy_points_ppr      numeric(8,2),
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (player_id, source_name, season, week)
);
CREATE INDEX IF NOT EXISTS ix_pws_player_season_week
    ON player_week_stats (player_id, season, week DESC);
COMMIT;
```

### 5.8 `017_ops_tables.sql`

```sql
BEGIN;
CREATE TABLE IF NOT EXISTS telegram_alerts (
    id bigserial PRIMARY KEY,
    alert_key text NOT NULL,          -- dedupe key
    league_id bigint REFERENCES leagues(id) ON DELETE CASCADE,
    alert_type text NOT NULL,
    body text,
    sent_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'sent',
    UNIQUE (alert_key)
);

CREATE TABLE IF NOT EXISTS waiver_claims (
    id bigserial PRIMARY KEY,
    league_id bigint NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week integer NOT NULL,
    waiver_target_id bigint REFERENCES waiver_targets(id) ON DELETE SET NULL,
    player_id bigint NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    dropped_player_id bigint REFERENCES players(id),
    faab_bid integer,
    outcome text,                     -- won | lost | pending | cancelled
    submitted_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    notes text
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id bigserial PRIMARY KEY,
    script_name text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running',   -- running | ok | error | empty
    rows_written integer,
    rows_skipped integer,
    error_text text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_sync_runs_recent
    ON sync_runs (script_name, started_at DESC);
COMMIT;
```

`sync_runs` is the direct antidote to §1's "every failure is silent." Every script writes a row, records skip counts, and a health check alerts when a script reports `status='empty'` or hasn't run.

### 5.9 `018_views.sql`

Rebuilds the views to be week-aware, use the `my_team_name` column with a payload fallback, and expose `lineup_status`. Full DDL omitted here for length; the substantive changes:

- `league_rosters`: change `is_my_team` to `COALESCE(l.my_team_name, l.payload->>'my_team_name')`; join projections on `week` as well as `source_name`; add `rsl.lineup_status`, `p.injury_status`, `pr.floor_points`, `pr.ceiling_points`.
- `waiver_wire`: same projection-join change. Note `is_my_team` does **not** currently exist on this view — adding it is a new column, not an edit.
- New `my_roster` view — `league_rosters WHERE is_my_team`, which is what the MCP server and engine actually query.
- New `opponent_moves` view for the alerting gap. This cannot be written as "`roster_status_changes` filtered on `NOT is_my_team`" — `roster_status_changes` has no `is_my_team` and does not join `leagues`. It needs:

  ```sql
  ... FROM roster_status_changes rsc
      JOIN leagues l ON l.id = rsc.league_id
     WHERE rsc.change_type IN ('add','drop')
       AND COALESCE(rsc.current_team, rsc.previous_team)
           IS DISTINCT FROM COALESCE(l.my_team_name, l.payload->>'my_team_name')
  ```

  **Known limitation:** `roster_status_changes` derives `add`/`drop` purely from `fantasy_team` NULL-transitions, so a player moving directly from one owner to another reports `no_change` unless the status also changed. Trades and owner-to-owner waiver claims are invisible. Fixing that means comparing `previous_team IS DISTINCT FROM current_team` as a fourth `change_type` (`transfer`) — worth doing in `018` while we're rewriting the view.

**Ordering constraint:** `CREATE OR REPLACE VIEW` can only append columns at the end, and these changes reorder columns. Also `league_rosters` and `waiver_wire` both depend on `roster_status_latest`. So `018` must be:

```sql
DROP VIEW IF EXISTS league_rosters, waiver_wire, roster_status_changes;
DROP VIEW IF EXISTS roster_status_latest;
-- then CREATE in dependency order: roster_status_latest first
```

### 5.10 `020_rankings_per_league.sql`

Everything §6.8 and the Phase 3 acceptance test need, plus the columns other sections assumed but no earlier migration created.

```sql
BEGIN;

-- rankings becomes per-league. This single change kills the duplicate-row bug,
-- the CFB cross-league wipe, and the cross-league contamination in waiver_planner.
ALTER TABLE rankings ADD COLUMN IF NOT EXISTS league_id   bigint REFERENCES leagues(id) ON DELETE CASCADE;
ALTER TABLE rankings ADD COLUMN IF NOT EXISTS source_name text;

-- Existing rows have no league attribution and were duplicated per league
-- anyway; they are not worth backfilling.
DELETE FROM rankings WHERE league_id IS NULL;
ALTER TABLE rankings ALTER COLUMN league_id SET NOT NULL;

-- sport/scoring_type are NOT NULL today and become redundant with league_id,
-- but keep them (denormalized) so existing read paths don't break.
CREATE UNIQUE INDEX IF NOT EXISTS ux_rankings_league_week_player
    ON rankings (league_id, week, player_id);

-- Alphanumeric external ids (Fantrax) — see 6.3. Stop base-36 mangling.
ALTER TABLE players ADD COLUMN IF NOT EXISTS external_player_key text;
CREATE UNIQUE INDEX IF NOT EXISTS ux_players_platform_extkey
    ON players (platform, external_player_key)
    WHERE external_player_key IS NOT NULL;

-- Normalized name for indexed matching — replaces the per-player
-- LIKE '%firstname%' scan in the current FantasyPros matcher (see 6.1).
ALTER TABLE players ADD COLUMN IF NOT EXISTS name_norm text
    GENERATED ALWAYS AS (
        lower(regexp_replace(player_name, '[^A-Za-z0-9 ]', '', 'g'))
    ) STORED;
CREATE INDEX IF NOT EXISTS ix_players_name_norm ON players (name_norm, pos);

COMMIT;
```

The generated-column expression is restricted to `lower()` + `regexp_replace()` because PG16 requires generated expressions to be IMMUTABLE. Accent folding via `unaccent()` is only STABLE and cannot be used here — if the matcher needs it, do that step in Python and store it in a plain column.

### 5.11 `021_roster_history_retention.sql` (optional, Phase 5)

The briefing puts `roster_status_history` at ~27 MB, append-only with no policy. (Confirm with the size query in §5.0 — the schema dump carries no size data.) Recommendation: **do not partition yet.**

1. Add a dedupe unique index to stop accidental double-runs. `league_id` is nullable, so `NULLS NOT DISTINCT` is required or orphan rows never dedupe:

   ```sql
   CREATE UNIQUE INDEX ux_rsh_snapshot ON roster_status_history
       (league_id, player_id, fetched_at) NULLS NOT DISTINCT;
   ```

   Better still, backfill and `SET NOT NULL` on `league_id` — a history row with no league is meaningless.
2. Collapse historical no-change rows: keep only rows where status changed from the prior snapshot, plus one row per player per week. Typically a 90%+ reduction, reversible only from backup, so dump first.
3. Revisit declarative partitioning by `fetched_at` month only if the table exceeds ~1 GB.

Partitioning requires a table rename, copy, and FK rebuild — materially riskier than the storage saving justifies at this size.

---

## 6. Per-script rewrite specifications

### 6.1 `scripts/common/` — new shared package

| Module | Contents |
|---|---|
| `constants.py` | `Platform`, `Sport`, `ScoringType`, `RosterStatus`, `LineupStatus`, `SourceName` enums. `PLATFORM_PROJECTION_SOURCE` mapping. Single source of truth for every literal. |
| `db.py` | Engine factory, `session_scope()`, and `run_tracked(script_name)` context manager that writes `sync_runs` and re-raises. Correct exception class: `sqlalchemy.exc.SQLAlchemyError`. |
| `matching.py` | One canonical player-matching implementation. Replaces the three divergent ones. See below. |
| `http.py` | Requests session with retry/backoff, ETag+`content_hash` caching into `sources`, and rate-limit accounting (CFBD Tier 2 has a monthly call budget worth tracking). |
| `teams.py` | Absorbs `teams_normalizer.py`, fixes the case-handling inconsistency (`TEAM_CODE_TO_NAME` probed with `.upper()`, `DST_LABEL_TO_NAME` probed case-sensitively) and the ambiguous-code resolution (`OSU`, `USC`, `MIA`, `MISS` currently resolve by dict order, not correctness). Adds a `team_key` canonical identity used by `matchup_schedule`. |

**`matching.py` design.** The current FantasyPros matcher is actively harmful because it always returns a match. Replacement:

1. Normalize both sides identically — strip punctuation, suffixes (`Jr`, `Sr`, `II`–`V`), lowercase, collapse whitespace. Store the normalized form in a generated column so SQL compares like-to-like.
2. Match in tiers with explicit confidence: exact `(norm_name, team, pos)` → 1.0; `(norm_name, pos)` → 0.9; `(norm_name)` → 0.75; fuzzy `rapidfuzz.token_set_ratio ≥ 92` with matching pos → scaled 0.6–0.85.
3. **Return `None` below a 0.70 floor.** Unmatched players go to a `match_review` queue, not a wrong xref.
4. Write every decision to `player_xref` with its confidence so bad matches are auditable and re-runnable.

Add a `players.name_norm` generated column plus index in a migration to make tier 1–3 index lookups rather than the current per-player `LIKE '%firstname%'` scan.

### 6.2 `sync_yahoo.py` — rewrite to JSON

**Step 1: discovery (you run this once).** Yahoo's internal endpoints are not publicly documented and I will not guess at URLs. New script `scripts/dev/discover_yahoo_endpoints.py`:

```python
# Attaches to the players page with your existing storage_state and records
# every XHR/fetch response, then writes a report of candidate JSON endpoints.
page.on("response", record)   # capture url, status, content-type, body sample
page.goto(players_url_for(league_id, pos="RB", start=0))
# ...paginate, change position, change status filter, open a team roster...
```

Output: `docs/yahoo_endpoints.md` listing each URL, its params, response shape, and where player id / ownership / roster slot live. **This is the one blocking unknown in the plan.** Everything in step 2 is conditional on what it finds.

Note the sandbox I'm working in has no network egress to Yahoo, so this step has to run on your host.

**Step 2: rewrite.** Once endpoints are known:

- Playwright is used only to establish and refresh the session. Data requests go through `context.request` (which shares cookies) or a plain `requests` session seeded from `storage_state`, which is far faster than page navigation per 25 players.
- Use Yahoo's real player id as `external_player_id`. This eliminates name-based matching for Yahoo, lets `ON CONFLICT (platform, external_player_id)` work properly, and makes the entire `_INVALID_TEAM_RE` / `is_valid_team_name` defensive layer deletable.
- Ownership comes from a structured field, not a `\bTeam\s+(...)` regex. Roster slot comes from a structured field into the new `lineup_status` / `slot_name` columns.
- **`unknown` status becomes a hard error.** If a player's ownership can't be determined, increment a skip counter, log the player, and fail the run if the skip rate exceeds ~2%. Silent partial data is worse than a failed run.
- Also sync league settings and the team list, so `leagues.my_team_name`, `my_team_external_id`, `team_count`, and `league_slots` come from Yahoo rather than the most-common-owner heuristic in `derive_my_team_name()`.
- Keep the hardened DOM path as an explicit `--fallback` mode that alerts loudly when used.

**Step 3: session durability.** `YAHOO_STATE_B64` expiry is a recurring manual chore. Add a preflight check that hits a cheap authenticated endpoint and, on 401/redirect-to-login, writes a `sync_runs` error and fires a Telegram alert telling you to re-run `encode_yahoo_state.py`. Also fix that script to validate its input is real Playwright state and to write to a file rather than printing a live credential to stdout.

### 6.3 `sync_fantrax.py`

- Resolve league by `(platform, external_league_id, season)`, not `order by id limit 1`.
- Create/upsert its own `leagues` row including `sport='NCAAF'`, `my_team_name`, and `league_slots`.
- **Stop base-36 mangling IDs.** Fantrax ids are alphanumeric strings. Add `players.external_player_key text` in a migration and use it for non-numeric platforms; keep `external_player_id bigint` for ESPN/Yahoo. Unique index on `(platform, external_player_key)`.
- Write `players.sport = 'NCAAF'` — this alone un-blocks CFBD xref and projections for the Fantrax league.
- Map real statuses to `lineup_status` (`starter`/`bench`/`ir`/`taxi`) instead of collapsing to `bench`.
- Use the current roster period, not `roster_periods[-1]`.
- `cast(:payload as jsonb)` like the ESPN scripts, and stop double-encoding `raw_row_text`.
- Fix the exception class.

### 6.4 `sync_espn.py` / `sync_espn_rosters.py`

- Write `platform='espn-nfl'`, `sport='NFL'` from constants.
- Paginate the player fetch (`limit`/`offset` loop) instead of `limit=2000`.
- Populate `my_team_name` and `my_team_external_id` by matching `SWID` against the league's team owners.
- Complete `POSITION_MAP` and reconcile it with `SLOT_MAP` on id 16 — pick `D/ST` or `DEF` once, in constants.
- Replace both `continue`-on-miss paths with counted, logged skips that surface in `sync_runs`.
- Capture `lineupSlotId` into `lineup_status`/`slot_name` rather than hardcoding `roster_status='owned'`.
- Guard `.get("playerPoolEntry", {})` against explicit `null` values with `or {}`.
- Hoist the per-player `pro_teams` bye-week lookup out of the loop into a dict.
- **Capture guillotine state:** `teams_remaining`, eliminated team names, and elimination week into `leagues.payload`. The survival model needs it.

### 6.5 `sync_fantasypros.py`

- **Send `week`.** Loop `week=0` (season) plus the current game week. This is the fix that makes weekly start/sit possible for NFL.
- Fetch all three scoring formats (`STD`, `HALF_PPR`, `PPR`) rather than hardcoding `'PPR'` into `scoring_format` — your leagues differ.
- Upsert on the full `ux_projections_key` expression list (see §5.2) instead of blind insert.
- Stop stuffing ECR into `news_summary` as `f"ECR={ecr}, tier={tier}..."` and re-parsing it downstream; use the `ecr_rank`/`ecr_tier` columns added in `011`.
- Request `DST` and map to whatever `pos` we canonicalize on, so the defense bonus fires.
- Pull `/nfl/{season}/news` into `player_events`, and player `injury_status` into `players.injury_status`.
- Use the new matcher; queue unmatched rather than force-matching.
- Verify the base path — code uses `/public/v2/json`, current docs say `/v2/json`.
- Note: the free tier is licensed for personal, non-production prototyping. Attribution is required if you publish analysis. Worth budgeting for HOF (~$9/mo annual) if this becomes something you rely on weekly.

### 6.6 CFBD: split into three scripts

`sync_cfbd_cfb_projections.py` is deleted. It conflates fetching with projecting and uses an actuals endpoint. Replacement:

| New script | Endpoint(s) | Writes |
|---|---|---|
| `sync_cfbd_reference.py` | `/teams`, `/roster`, `/calendar`, `/games` | `pro_teams`, `player_xref` (properly, with confidence), `matchup_schedule` skeleton |
| `sync_cfbd_usage.py` | `/player/usage`, `/ppa/players/season`, `/ppa/players/games`, `/games/players`, `/player/returning` | `player_week_stats` (actuals + usage + PPA) |
| `sync_cfbd_context.py` | `/lines`, `/stats/season/advanced`, `/metrics/wp/pregame` | `matchup_schedule` (vegas spread/total, `implied_points`, opponent defensive PPA) |

All three: `ON CONFLICT DO UPDATE`, never delete-by-`(source, season)`. Track call count against the Tier 2 monthly budget in `http.py`. Empty response is a `sync_runs` status of `empty` plus an alert, never `sys.exit(1)`.

Also fix `sync_cfbd_player_xref.py`: actually write `player_xref`, re-evaluate low-confidence mappings instead of filtering on `cfbd_athlete_id IS NULL`, index the CFBD roster by normalized name to kill the O(players × index) scan, apply the position filter on the exact-match path too, and `CREATE TABLE IF NOT EXISTS` the overrides table.

### 6.7 `build_projections.py` — new, the real CFB model

CFBD sells data, not projections, so the projections have to be built. Model:

```
projected_points(player, week)
  = Σ_stat  E[stat] × league_scoring_coefficient(stat)

E[volume_stat]  = usage_rate_shrunk × team_plays_projected × opportunity_share
E[efficiency]   = ppa_per_opportunity_shrunk × opponent_defense_adjustment
team_plays_projected, team_points_projected
                ← matchup_schedule.implied_points  (from Vegas spread + total)
```

Three design points that matter:

**Bayesian shrinkage on small samples.** Week 1–3 college football has almost no current-season data, and a running back with 4 carries for 60 yards is not a 15 YPC player. Shrink each rate toward a position-and-team prior with weight `n / (n + k)`, where the prior comes from prior-season production and `/player/returning`, and `k` is tuned per stat (~40 opportunities for rushing efficiency, ~25 for target share). Without this, early-season CFB projections are noise and the recommendations will be actively bad.

**Vegas as the team-context anchor.** `implied_points = total/2 - spread/2` is the best available single estimate of how many points a team scores this week, and it prices in injuries, weather, and matchup automatically. Distributing a team's implied points across its players by usage share is more accurate than any bottom-up stat model at this data volume — and CFB's enormous talent gaps (a 45-point favorite) make this especially true.

**Distributions, not point estimates.** Compute `std_dev` from the player's own week-to-week variance, shrunk toward a position baseline, then `floor_points` / `ceiling_points` as the 20th/80th percentiles. §7 needs these; a mean alone cannot support a guillotine survival objective.

Scoring conversion stays in `cfb_scoring.py`, but fix it: `two_pt_conversions` and `fumble_return_td` are dead terms never emitted by the normalizer, and there is no K or DST scoring at all — so kickers and defenses currently project to 0 and rank last while `waiver_planner.py` still tries to fill `K` and `D/ST` slots from `league_slots`. Yahoo college fantasy also has a **Team Offense** position that isn't handled anywhere; see §11.

For NFL, `build_projections.py` blends FantasyPros weekly projections (primary) with an opponent adjustment from defense-vs-position and the same Vegas anchor, and derives the same distribution columns.

### 6.8 `build_rankings.py` — rewrite

- Read from constants; no inline platform literals.
- Use the per-league `rankings` shape from migration `020` — `league_id`, `source_name`, unique on `(league_id, week, player_id)`. This kills the duplicate-row bug, the CFB cross-league wipe, and the cross-league contamination in one change: rankings become per-league, which is what every consumer actually wants.
- Filter projections by `season` **and** `week` in the `latest_proj` CTE.
- One season env var (`FANTASY_SEASON`), not `ESPN_SEASON` vs `CFBD_SEASON`.
- Populate `opp_team` and `def_strength` from `matchup_schedule`.
- Zero out projections for players on bye (`matchup_schedule.is_bye`) — currently a bye-week player can be recommended as a start.

### 6.9 `recommend.py` — replaces `waiver_planner.py`

Deterministic engine. The LLM explains and can override, but the math is reproducible.

**Optimal lineup.** Current `compute_start_sit` is a greedy fill writing `slot = "SLOT-{idx}"`. Replace with a proper assignment over real slots from `league_slots`, respecting multi-eligibility (FLEX, superflex) — a small max-weight bipartite matching (`scipy.optimize.linear_sum_assignment`, or a hand-rolled Hungarian since rosters are tiny). Greedy demonstrably mis-fills FLEX when a player is eligible at two positions.

**Objective, per league type:**

- *Standard/H2H:* maximize expected points against the opponent's projected total; when a large underdog, shift weight toward ceiling.
- *Guillotine:* maximize `P(not lowest score this week)`. Simulate: draw each player's score from its distribution, sum, compare against the distribution of the other `teams_remaining - 1` teams' projected totals. Early season (many teams) this strongly favors floor; late season it flips to ceiling. Objective weight is a function of `teams_remaining`.

**Waiver value = replacement-level delta, not raw projected points.** A player is worth adding by how much they improve your *starting lineup* over the rest of the season versus the worst player you'd start at that slot — not by their projection in isolation. Current logic ranks by projected points, which systematically overvalues depth at positions you're already strong at.

**FAAB bid sizing** from `faab_budget`, weeks remaining, and marginal value — currently unmodelled despite `faab_budget` being in the schema.

**Guillotine waiver timing.** Each week's eliminated roster dumps into free agency. `recommend.py` should treat the elimination event as a scheduled opportunity and pre-rank the at-risk teams' rosters before elimination resolves.

**Drop candidates** must respect `in_week_adds_allowed`, `add_drop_lock_window`, and bye weeks, and must never recommend dropping a player who is the only body at a required slot.

### 6.10 `sync_player_news.py` — new

Hourly poller into `player_events`, deduped by `content_hash`. Sources: FantasyPros news endpoint, ESPN player news, and for CFB the depth-chart/injury gap noted in the briefing. Feeds `players.injury_status` and Telegram alerts. Alerts are recorded in `telegram_alerts` keyed by `alert_key` so a restart doesn't re-fire.

---

## 7. MCP server for the local model

`app/mcp/server.py`, exposed over stdio (local) or SSE (if the Mac Mini is remote from the DB host). Read-only DB role.

Design principle: **tools return decisions and evidence, not tables.** A 24GB Mac Mini running a quantized 14–32B model has limited context headroom, so every payload is pre-filtered, pre-ranked, and capped. No tool returns more than ~40 rows or ~4KB of JSON by default.

| Tool | Args | Returns |
|---|---|---|
| `list_leagues` | — | id, name, platform, sport, scoring, week, `teams_remaining`, my team name |
| `get_league_rules` | `league_id` | slots, scoring coefficients, waiver type, FAAB remaining, lock windows, guillotine flag |
| `get_my_roster` | `league_id`, `week?` | per player: name, pos, slot, projection + floor/ceiling, opponent, implied team total, injury status, bye flag |
| `get_optimal_lineup` | `league_id`, `week?` | engine's assignment, the points/survival delta vs current lineup, and per-swap reasoning inputs |
| `get_waiver_candidates` | `league_id`, `week?`, `pos?`, `limit=20` | ranked by replacement-value delta, with suggested drop and FAAB bid |
| `get_drop_candidates` | `league_id`, `week?` | with the constraint checks that were applied |
| `get_matchup` | `league_id`, `week?` | opponent roster and projected totals, or guillotine field distribution |
| `get_player_detail` | `player_id` | recent `player_week_stats` trend, usage trajectory, recent `player_events` |
| `get_player_news` | `league_id?`, `since?` | recent events affecting rostered/watched players |
| `get_opponent_moves` | `league_id`, `since?` | recent adds/drops by other teams |
| `explain_projection` | `player_id`, `week` | the model's inputs — usage, efficiency, implied points, opponent adjustment, shrinkage weight |
| `get_data_health` | — | `sync_runs` summary: what ran, what was empty, what's stale |

`explain_projection` and `get_data_health` exist so the model can tell the difference between "this player is projected low" and "we have no data for this player." That distinction is exactly what the current silent-failure architecture destroys, and without it the model will confidently recommend against players it simply knows nothing about.

Recommended local models at 24GB: a 14B-class instruct model at Q5/Q6 leaves comfortable KV-cache room for multi-turn tool use; a 32B at Q4 fits but will feel tight with long tool transcripts. Prefer strong tool-calling over raw size — the reasoning here is light because §6.9 does the math.

---

## 8. Automation

Add a `fantasy-scheduler` service to `compose.yaml` running APScheduler (simpler than Celery, no broker needed, and this is a single-host cron replacement).

| Job | Cadence | Notes |
|---|---|---|
| Roster sync (all 4 leagues) | every 30 min, game days every 10 min | writes `sync_runs` |
| Player news | hourly | → `player_events` → Telegram |
| FantasyPros projections | daily 06:00, plus Tue/Wed/Sat | week + season |
| CFBD usage/actuals | Sun+Mon (after games settle) | |
| CFBD lines/context | daily, then T-3h before kickoff | lines move |
| `build_projections` → `build_rankings` → `recommend` | after each projection refresh | chained, fail-fast |
| Waiver reminder | league-specific, before `waiver_clear_time` | |
| Session-health preflight | every 6h | Yahoo/ESPN/Fantrax cookie expiry alerts |
| Data-health check | daily | alerts on `status='empty'` or stale scripts |

Every job is wrapped in `run_tracked()`. CI already lints (black/isort/flake8/eslint) — add a `pytest` job that at minimum covers `matching.py`, `cfb_scoring.py`, the lineup optimizer, and a migration-applies-cleanly check against a throwaway Postgres container.

---

## 9. Phasing

Each phase is independently shippable and has a concrete pass/fail test.

### Phase 1 — Stop the silent failures (highest value, lowest risk)
✅ `009` preflight complete, collisions clear. Then migrations `010`–`013` plus `018` (which must ship with `013`). Create `scripts/common/`. Point every existing script at constants. Add `sync_runs` tracking and fix the exception classes.

**Promoted into Phase 1 by the preflight** (was Phase 3): fix `sync_fantasypros.py` — the `week` param, all three scoring formats, and a matcher that returns `None` rather than guessing. With only 47 NFL players loaded (§1.6), the ESPN leagues have no usable projections regardless of what the schema says, so this can't wait.

Also promoted: **add a free-agent pass to `sync_espn_rosters.py`** (§1.7). Both guillotine leagues currently have zero waiver data, which is the one thing that format is actually about.

**Acceptance:** `players.sport` non-null everywhere. Every script writes a `sync_runs` row. `league_rosters` projection coverage above 80% for all four leagues — the honest test, since the platform rename alone moves it to ~11% for ESPN. `waiver_wire` returns rows for both ESPN leagues. `player_xref` has no many-to-one collapses onto a single `player_id`.

### Phase 2 — Yahoo ingestion
Discovery script → `docs/yahoo_endpoints.md` → rewrite `sync_yahoo.py` on JSON. Session-health preflight.

**Acceptance:** Yahoo players have non-null `external_player_id`; zero rows with `roster_status='unknown'`; `fantasy_team` values all match a real Yahoo team name; skip rate under 2%; `leagues.my_team_name` and `league_slots` populated from Yahoo rather than inferred.

### Phase 3 — Real projections
Migrations `014`–`017` and `020`. FantasyPros week param + all scoring formats. Split CFBD into three scripts. Build `build_projections.py`. Rewrite `build_rankings.py`.

**Acceptance:** `projections` has rows for `week = <current>` for all four leagues, with non-null `floor_points`/`ceiling_points`. `matchup_schedule` has `implied_points` for the current week. `rankings` has exactly one row per `(league_id, week, player_id)`. Backtest: projections for weeks already played correlate with `player_week_stats` actuals better than a naive season-average baseline — if they don't, the model is worse than nothing and needs work before Phase 4 trusts it.

### Phase 4 — Recommendation engine
`recommend.py` with the lineup optimizer, replacement-value waivers, FAAB sizing, and the guillotine survival objective.

**Acceptance:** For a past week, the engine's optimal lineup scores ≥ what you actually started. Waiver recommendations are reproducible run-to-run. No recommendation violates a league rule (bye, lock window, roster minimum).

### Phase 5 — MCP + AI layer
MCP server, local model wiring, Telegram bot, scheduler service. Then optional `021` retention work.

**Acceptance:** The local model answers "who should I start in the Yahoo EDIT league this week and why" using only tool calls, and its reasoning cites actual projection inputs via `explain_projection`.

---

## 10. What I need from you

**Received 2026-09-08** — all league settings, now transcribed to `config/leagues.yaml`. That resolved four earlier questions: both ESPN leagues are confirmed knockout/guillotine (10 and 18 teams, weekly-score elimination); the Yahoo EDIT League does **not** use Team Offense; Yahoo and Fantrax are both waiver-**priority**, not FAAB, so FAAB bid sizing applies only to the two ESPN leagues; and the ESPN Pick'em group is a fifth league that must be excluded from the roster pipeline.

Still blocking:

1. **Run the §5.0 preflight** (`sql/preflight/009_preflight.sql`) and send me `preflight_out.txt`. `init.sql` is schema-only, so I can't see stored values. Migration `010` is a data rewrite and two statements can abort on a unique-constraint collision.
2. **Take a `pg_dump` before migration `010`.** It backfills and rewrites live rows.
3. **Run the Yahoo discovery script** (Phase 2 blocker). I can't reach Yahoo from my sandbox and won't invent endpoint URLs.
4. **Confirm the two ESPN `external_league_id` values** — `719429857` and `209442251` come from the compose default; which is TX Guillotine and which is The_Dark_Side? Preflight §4 will show what's stored.
5. **Confirm the FantasyPros base path** your key works against (`/public/v2/json` vs `/v2/json`), and whether to stay on the free tier given its non-production licensing.
6. **Season consolidation.** Defaults are split across `YAHOO_SEASON`, `ESPN_SEASON`, `CFBD_SEASON`, all 2026. Confirm we collapse to one `FANTASY_SEASON`.
7. **Fantrax "New Freshmen" — is the player pool restricted to freshmen**, or just to those 73 teams? The settings page only shows the team restriction. If it's genuinely freshmen-only, projections need a class filter and CFBD `/roster` gives us `year`.
8. **`teams_remaining` for both knockout leagues** needs to be current each week. Best solved by having `sync_espn.py` derive it rather than you maintaining it by hand — but confirm ESPN exposes eliminated status in the payload.

---

## 11. Open technical risks

| Risk | Mitigation |
|---|---|
| Yahoo internal JSON endpoints change or don't exist in a usable form | Discovery script runs first; hardened DOM fallback retained; this is why Phase 2 is scoped separately |
| Yahoo scraping is against Yahoo's ToS | Personal use on your own league data, low request volume, aggressive caching. Worth knowing; your call |
| CFBD Tier 2 monthly call budget | `http.py` tracks and caps call counts; ETag caching; reference data fetched weekly not hourly |
| CFB projections are genuinely hard early season | Bayesian shrinkage + Vegas anchor; Phase 3 acceptance gate is a backtest against a naive baseline, so we find out before trusting it |
| FantasyPros free tier is non-production licensed | Documented; HOF upgrade path noted |
| Migration `010` rewrites live rows | `pg_dump` first; each migration is transactional with a rollback block and a verification query |
