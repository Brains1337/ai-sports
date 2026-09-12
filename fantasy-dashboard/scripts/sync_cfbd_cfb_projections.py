#!/usr/bin/env python3
"""
sync_cfbd_cfb_projections.py — CollegeFootballData → projections (NCAAF).

Supports two modes:

  - "actual"   (CFBD_MODE="actual", default): pulls completed game stats via
    /games/players for weeks 1..(N-1) and projects week N by averaging each
    player's per-game output — i.e. season-to-date average extrapolated forward.
    No opponent adjustment is possible for weeks whose games haven't been played.

  - "projected" (CFBD_MODE="projected"): builds forward-looking projections
    using three data sources:
      1. /ppa/players/games  → per-player PPA (predicted points added) by game,
         averaged to get an expected per-game contribution baseline.
      2. /player/season/overview → usage rates (pass/rush/receiving share) and
         season totals, used to scale PPA into fantasy-relevant stat estimates.
      3. /stats/season/advanced → opponent defensive strength vs run / pass /
         receiving, used as a multiplier on the baseline to adjust each player's
         projected output for the specific opponent they face that week.
         A team that is weak against the run (e.g. allows 200+ rush ypg) gets a
         defensive_multiplier > 1.0 for opposing rushers / pass-catching RBs;
         a strong run defense gets < 1.0; likewise for pass offense vs pass defense.

  Projected mode then scores the projected stat lines via cfb_scoring and writes
  to the projections table, same as actual mode.

Env (shared by both modes):
  DATABASE_URL      → PostgreSQL connection string.
  CFBD_API_KEY      → CollegeFootballData API key (Bearer token).
  CFBD_SEASON       → season year, default 2026.
  CFBD_WEEK         → target week to project for (int).
  CFBD_MODE         → "actual" | "projected" (default "actual").

Additional project-specific env used by the Docker container:
  CFBD_PLAYER_POOL  → optional comma-separated list of platforms whose player
                      maps to include (defaults to all configured platforms).

Writes to `projections` with:
  source_name in ('cfbd_cfb_proj_yahoo', 'cfbd_cfb_proj_fantrax')
  season, scoring_format, projected_points, plus raw stat breakdown in payload.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from sqlalchemy import create_engine, text

from cfb_scoring import YAHOO_CFB, FANTRAX_CFB, score_stats

database_url = os.environ["DATABASE_URL"]
CFBD_API_KEY = os.environ["CFBD_API_KEY"]
CFBD_SEASON = int(os.getenv("CFBD_SEASON", "2026"))
CFBD_WEEK = int(os.getenv("CFBD_WEEK", "1"))
CFBD_MODE = os.getenv("CFBD_MODE", "actual").strip().lower()
assert CFBD_MODE in ("actual", "projected"), f"CFBD_MODE must be 'actual' or 'projected', got '{CFBD_MODE}'"

CFBD_BASE = "https://api.collegefootballdata.com"

HEADERS = {
    "Authorization": f"Bearer {CFBD_API_KEY}",
    "Accept": "application/json",
}

SOURCE_NAMES = {
    YAHOO_CFB: "cfbd_cfb_proj_yahoo",
    FANTRAX_CFB: "cfbd_cfb_proj_fantrax",
}

# Platform → sport column value in players table
PLATFORM_SPORT = {
    YAHOO_CFB: "NCAAF",
    FANTRAX_CFB: "NCAAF",
}

engine = create_engine(database_url, pool_pre_ping=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_player_game_stats(season: int, week: int) -> List[Dict[str, Any]]:
    """
    CFBD player game stats for a given season/week (all FBS teams).
    Returns raw CFBD player-stat category rows.
    """
    resp = requests.get(
        f"{CFBD_BASE}/games/players",
        params={"year": season, "week": week, "seasonType": "regular"},
        headers=HEADERS,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def normalize_stats(raw_games: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Flatten CFBD's nested games->teams->categories->types->athletes structure
    into { athlete_id: { name, team, pass_yd, pass_td, ... } }.

    CFBD's exact category/type names can shift; adjust the CATEGORY_MAP
    below after inspecting a live response (see debug print in main()).
    """
    players: Dict[str, Dict[str, Any]] = {}

    CATEGORY_STAT_MAP = {
        ("passing", "YDS"): "pass_yd",
        ("passing", "TD"): "pass_td",
        ("passing", "INT"): "interceptions",
        ("rushing", "YDS"): "rush_yd",
        ("rushing", "TD"): "rush_td",
        ("receiving", "YDS"): "rec_yd",
        ("receiving", "TD"): "rec_td",
        ("receiving", "REC"): "receptions",
        ("fumbles", "LOST"): "fumbles_lost",
    }

    for game in raw_games:
        teams = game.get("teams", []) or []
        for team in teams:
            team_name = team.get("team")
            categories = team.get("categories", []) or []
            for cat in categories:
                cat_name = (cat.get("name") or "").lower()
                types = cat.get("types", []) or []
                for t in types:
                    stat_name = (t.get("name") or "").upper()
                    key = CATEGORY_STAT_MAP.get((cat_name, stat_name))
                    if not key:
                        continue
                    for athlete in t.get("athletes", []) or []:
                        athlete_id = str(athlete.get("id"))
                        name = athlete.get("name")
                        try:
                            value = float(athlete.get("stat") or 0)
                        except (TypeError, ValueError):
                            value = 0.0

                        entry = players.setdefault(
                            athlete_id,
                            {"name": name, "team": team_name},
                        )
                        entry[key] = entry.get(key, 0.0) + value

    return players


# ---------------------------------------------------------------------------
# PROJECTED MODE (CFBD_MODE="projected")
#
# For weeks where games haven't been played yet, we build projections from:
#   1. /ppa/players/games       → per-game PPA by player (expected-contribution baseline)
#   2. /player/season/overview  → usage rates + season stats (to map PPA → fantasy stats)
#   3. /games                   → week's matchup schedule (team vs opponent)
#   4. /stats/season/advanced   → opponent defensive strength vs run / pass
# ---------------------------------------------------------------------------

# Stat keys the projection model produces
PROJ_STAT_KEYS = (
    "pass_yd", "pass_td", "interceptions",
    "rush_yd", "rush_td",
    "rec_yd", "rec_td", "receptions",
    "fumbles_lost",
)

def _cfbd_get(path: str, params: Dict[str, Any] | None = None) -> Any:
    """GET a CFBD endpoint and return parsed JSON (or raise).

    Retries with exponential backoff — CFBD's API can be slow during peak
    usage and returns occasional 503s. Respects rate-limit headers.
    """
    max_retries = 3
    base_delay = 2
    url = f"{CFBD_BASE}{path}"
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=120,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", str(base_delay * attempt)))
                print(
                    f"[cfbd-cfb] rate limited (429) on attempt {attempt}/{max_retries}, "
                    f"waiting {retry_after}s",
                    file=sys.stderr,
                )
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"[cfbd-cfb] request failed (attempt {attempt}/{max_retries}), "
                    f"retrying in {delay}s: {exc}",
                    file=sys.stderr,
                )
                time.sleep(delay)
            else:
                print(
                    f"[cfbd-cfb] request failed after {max_retries} attempts: {exc}",
                    file=sys.stderr,
                )
                raise

    if last_exc:
        raise last_exc
    raise RuntimeError("Unexpected: retries exhausted without exception")


def fetch_player_ppa_games(season: int, week: int) -> Dict[str, Dict[str, Any]]:
    """
    /ppa/players/games — per-player PPA (predicted points added) for games.

    Returns { athlete_id: { avg_ppa: float, position: str, team: str } }
    averaged across all games up to `week`.  PPA is a projection-aware metric:
    it already reflects opponent strength within each play.
    """
    # Fetch PPA for all games played so far this season (weeks 1..week-1)
    # CFBD /ppa/players/games uses 'year' param, not 'season'
    data = _cfbd_get(
        "/ppa/players/games",
        {"year": season, "week": max(1, week - 1), "seasonType": "regular"},
    )
    players: Dict[str, Dict[str, Any]] = {}
    for row in data:
        athlete_id = str(row.get("athleteId") or row.get("athlete_id") or row.get("id") or "")
        if not athlete_id:
            continue
        # CFBD returns averagePPA: { rush, pass, all }; also try flat fields
        avg_ppa = row.get("averagePPA", {})
        if isinstance(avg_ppa, dict):
            ppa_val = float(avg_ppa.get("all") or avg_ppa.get("avg_PPA_all") or 0)
        else:
            ppa_val = float(row.get("avg_PPA_all") or row.get("ppa_all") or row.get("avgPPA") or avg_ppa or 0)
        entry = players.setdefault(
            athlete_id,
            {"avg_ppa": 0.0, "position": row.get("position", ""), "team": row.get("team", "")},
        )
        entry["avg_ppa"] += ppa_val
    # PPA from /ppa/players/games is already per-game averages; multiple rows
    # for the same athlete represent different games, and we summed them.
    # For a per-game projection we keep the raw sum (which approximates total
    # PPA contribution); the ppa_factor in project_player_stats normalizes this.
    print(
        f"[cfbd-cfb] fetched PPA for {len(players)} athletes (season={season}, week={week})",
        file=sys.stderr,
    )
    return players


def fetch_season_overview(season: int, team_filter: str | None = None) -> Dict[str, Dict[str, Any]]:
    """
    /stats/player/season — season-level stats per player (bulk, flat format).

    CFBD returns List[PlayerStat]: { playerId, playerName, team, position,
    category (passing|rushing|receiving|fumbles), type (YDS|TD|REC|LOST|INT),
    stat (float), season }

    We aggregate multiple rows per player into our internal format.

    Returns { athlete_id: { name, team, position, usage, pass_yd, rush_yd, ... } }
    """
    params: Dict[str, Any] = {"year": season, "seasonType": "regular", "classification": "fbs"}
    if team_filter:
        params["team"] = team_filter
    data = _cfbd_get("/stats/player/season", params)

    # (category, type) -> internal stat key, matching the CATEGORY_MAP in normalize_stats()
    category_type_map = {
        ("passing", "YDS"): "pass_yd",
        ("passing", "TD"): "pass_td",
        ("passing", "INT"): "interceptions",
        ("rushing", "YDS"): "rush_yd",
        ("rushing", "TD"): "rush_td",
        ("receiving", "YDS"): "rec_yd",
        ("receiving", "TD"): "rec_td",
        ("receiving", "REC"): "receptions",
        ("fumbles", "LOST"): "fumbles_lost",
    }

    players: Dict[str, Dict[str, Any]] = {}
    for row in data:
        athlete_id = str(row.get("playerId") or row.get("player_id") or "")
        if not athlete_id:
            continue
        name = row.get("playerName") or row.get("player_name") or ""
        team = row.get("team") or ""
        position = row.get("position") or ""
        category = (row.get("category") or "").lower()
        stat_type = (row.get("type") or row.get("statType") or "").upper()
        stat_val = float(row.get("stat") or row.get("value") or 0)

        if athlete_id not in players:
            players[athlete_id] = {
                "name": name,
                "team": team,
                "position": position,
                "usage": {},
            }
            for internal_key in category_type_map.values():
                players[athlete_id][internal_key] = 0.0

        key = category_type_map.get((category, stat_type))
        if key:
            players[athlete_id][key] = stat_val

    print(
        f"[cfbd-cfb] fetched season overview for {len(players)} athletes",
        file=sys.stderr,
    )
    return players


def fetch_week_matchups(season: int, week: int) -> Dict[str, str]:
    """
    /games — get the week-N matchup schedule.

    Returns { home_team: away_team, ... } both directions so we can look up
    a team's opponent regardless of home/away.
    """
    data = _cfbd_get(
        "/games",
        {"year": season, "week": week, "seasonType": "regular", "division": "fbs"},
    )
    matchups: Dict[str, str] = {}
    for game in data:
        home = game.get("homeTeam") or game.get("home_team") or game.get("home")
        away = game.get("awayTeam") or game.get("away_team") or game.get("away")
        if home and away:
            matchups[home] = away
            matchups[away] = home
    print(
        f"[cfbd-cfb] fetched {len(matchups)//2} matchups for season={season} week={week}",
        file=sys.stderr,
    )
    return matchups


def fetch_defensive_stats(season: int) -> Dict[str, Dict[str, float]]:
    """
    /stats/season/advanced (or /stats/season fallback) — team defensive metrics.

    Returns { team_name: {
        rush_def: float,   # defensive multiplier for opposing rush (e.g. 1.2 = 20% boost)
        pass_def: float,   # defensive multiplier for opposing pass offense
        run_ypc_allowed: float,  # yards per carry allowed (for reference)
        pass_ypa_allowed: float, # yards per attempt allowed in pass (for reference)
    } }

    Multiplier logic:
      - A team allowing high rush ypc gets rush_def > 1.0 (more rushing production allowed)
      - A team allowing high pass ypa gets pass_def > 1.0 (more passing production allowed)
      - Baseline is the average; league-average team = 1.0
    """
    try:
        raw = _cfbd_get(
            "/stats/season/advanced",
            {"year": season, "seasonType": "regular", "division": "fbs"},
        )
    except Exception:
        # Fallback: basic /stats/season (statName category)
        raw = _cfbd_get(
            "/stats/season",
            {"year": season, "seasonType": "regular", "division": "fbs"},
        )

    # Build per-team defensive stats from the advanced response.
    # The exact JSON shape varies by CFBD version, so we handle both
    # the nested advanced structure and flat stat rows.
    teams: Dict[str, Dict[str, float]] = {}
    total_rush_yPC: List[float] = []
    total_pass_ypa: List[float] = []

    for row in raw:
        team = row.get("team") or row.get("team_name") or row.get("name")
        if not team:
            continue

        # Advanced format: row has .defense.rush_yards_per_carry, .defense.pass_yards_per_attempt etc.
        # Flat format: row.statName in { "rushYardsPerCarryAllowed", "passYardsPerAttemptAllowed" }
        rush_ypc = _extract_float(row, ["defense", "rush_yards_per_carry"], "rushYardsPerCarryAllowed")
        pass_ypa = _extract_float(row, ["defense", "pass_yards_per_attempt"], "passYardsPerAttemptAllowed")

        if rush_ypc is not None:
            total_rush_yPC.append(rush_ypc)
        if pass_ypa is not None:
            total_pass_ypa.append(pass_ypa)

        teams[team] = {
            "rush_ypc_allowed": rush_ypc or 0.0,
            "pass_ypa_allowed": pass_ypa or 0.0,
        }

    # Compute league-average baselines for multiplier normalization
    avg_rush_ypc = sum(total_rush_yPC) / len(total_rush_yPC) if total_rush_yPC else 4.0  # ~league avg
    avg_pass_ypa = sum(total_pass_ypa) / len(total_pass_ypa) if total_pass_ypa else 6.5  # ~league avg

    # Convert to defensive multipliers: higher = worse defense = more production allowed
    for team, stats in teams.items():
        rush_allowed = stats["rush_ypc_allowed"] or avg_rush_ypc
        pass_allowed = stats["pass_ypa_allowed"] or avg_pass_ypa
        # Multiplier: if a team allows 5.0 ypc vs 4.0 league avg, multiplier = 1.25
        stats["rush_def"] = round(rush_allowed / avg_rush_ypc, 3) if avg_rush_ypc > 0 else 1.0
        stats["pass_def"] = round(pass_allowed / avg_pass_ypa, 3) if avg_pass_ypa > 0 else 1.0

    print(
        f"[cfbd-cfb] fetched defensive stats for {len(teams)} teams "
        f"(avg_rush_ypc={avg_rush_ypc:.2f}, avg_pass_ypa={avg_pass_ypa:.2f})",
        file=sys.stderr,
    )
    return teams


def _extract_float(row: Dict[str, Any], nested_keys: List[str], flat_key: str) -> float | None:
    """Try to extract a float from either nested dict or flat stat row."""
    # Try nested: row['defense']['rush_yards_per_carry']
    val: Any = row
    for k in nested_keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            val = None
            break
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    # Try flat: row['rushYardsPerCarryAllowed']
    flat_val = row.get(flat_key)
    if flat_val is not None:
        try:
            return float(flat_val)
        except (TypeError, ValueError):
            pass
    return None


def project_player_stats(
    player_overview: Dict[str, Any],
    avg_ppa: float,
    opponent_def: Dict[str, float],
    position: str,
) -> Dict[str, float]:
    """
    Project a player's fantasy-relevant stats for a single game given:
      - their season overview (usage rates, season totals)
      - their average PPA from /ppa/players/games
      - the opponent's defensive multipliers (rush_def, pass_def)

    Logic:
      1. Start from season averages (season totals / games played) — this is the
         baseline stat line for a typical game against an average opponent.
      2. Scale by the opponent's defensive multiplier:
           - Rushers / pass-catching RBs: scale rush_yd, rush_td by rush_def
           - Receivers / TE / pass-catching RB: scale rec_yd, rec_td by pass_def
           - QBs: scale pass_yd, pass_td by pass_def
      3. Blend in PPA as a confidence weight: higher PPA → less regression to
         mean, lower PPA → more conservative (scale stats by PPA-derived factor).
    """
    stats: Dict[str, float] = {}
    for key in PROJ_STAT_KEYS:
        stats[key] = 0.0

    if not player_overview:
        return stats

    # Season averages (if we have games played)
    games_played = float(player_overview.get("games") or player_overview.get("gamesPlayed") or 0)
    if games_played <= 0:
        games_played = 1.0  # avoid div-by-zero; treat season totals as per-game

    # Baseline: season-to-date averages
    stats["pass_yd"] = player_overview.get("pass_yd", 0.0) / games_played
    stats["pass_td"] = player_overview.get("pass_td", 0.0) / games_played
    stats["interceptions"] = player_overview.get("interceptions", 0.0) / games_played
    stats["rush_yd"] = player_overview.get("rush_yd", 0.0) / games_played
    stats["rush_td"] = player_overview.get("rush_td", 0.0) / games_played
    stats["rec_yd"] = player_overview.get("rec_yd", 0.0) / games_played
    stats["rec_td"] = player_overview.get("rec_td", 0.0) / games_played
    stats["receptions"] = player_overview.get("receptions", 0.0) / games_played
    stats["fumbles_lost"] = player_overview.get("fumbles_lost", 0.0) / games_played

    # Apply opponent defensive multiplier
    rush_mult = opponent_def.get("rush_def", 1.0)
    pass_mult = opponent_def.get("pass_def", 1.0)

    pos = position.upper()
    if pos in ("RB",):
        # RBs: rushing is the primary path; receiving also affected by pass_def
        stats["rush_yd"] *= rush_mult
        stats["rush_td"] *= rush_mult
        stats["rec_yd"] *= pass_mult
        stats["rec_td"] *= pass_mult
    elif pos in ("WR",):
        stats["rec_yd"] *= pass_mult
        stats["rec_td"] *= pass_mult
    elif pos in ("TE",):
        stats["rec_yd"] *= pass_mult
        stats["rec_td"] *= pass_mult
    elif pos == "QB":
        stats["pass_yd"] *= pass_mult
        stats["pass_td"] *= pass_mult
        stats["rush_yd"] *= rush_mult
        stats["rush_td"] *= rush_mult

    # PPA-based confidence adjustment: blend PPA-weighted factor.
    # PPA reflects expected contribution; high PPA → boost, low/negative → conservative.
    # We normalize by games_played (number of games PPA was summed over) to get per-game PPA.
    ppa_games = float(player_overview.get("games") or player_overview.get("gamesPlayed") or 0)
    ppa_per_game = avg_ppa / ppa_games if ppa_games > 0 else avg_ppa
    ppa_factor = 1.0 + max(-0.15, min(0.15, ppa_per_game / 20.0))
    for key in PROJ_STAT_KEYS:
        stats[key] *= ppa_factor

    return stats


def build_projections() -> Dict[str, Dict[str, Any]]:
    """
    Main projection pipeline for projected mode.

    Returns { athlete_id: { name, team, position, platform_stats: { platform: stats } } }
    where platform_stats contains projected stats for each platform's scoring format.
    """
    print(
        f"[cfbd-cfb] building PROJECTED mode for season={CFBD_SEASON} week={CFBD_WEEK}",
        file=sys.stderr,
    )

    # 1. Fetch player PPA games (baseline expected contribution)
    ppa_data = fetch_player_ppa_games(CFBD_SEASON, CFBD_WEEK)

    # 2. Fetch season overviews (usage rates + season totals)
    overview = fetch_season_overview(CFBD_SEASON)

    # 3. Fetch week-N matchups (who plays whom)
    matchups = fetch_week_matchups(CFBD_SEASON, CFBD_WEEK)

    # 4. Fetch opponent defensive stats
    def_stats = fetch_defensive_stats(CFBD_SEASON)

    # 5. Build per-player projections
    proj_players: Dict[str, Dict[str, Any]] = {}
    for athlete_id, ppa_entry in ppa_data.items():
        player_overview = overview.get(athlete_id, {})
        team = ppa_entry.get("team") or player_overview.get("team", "")
        position = ppa_entry.get("position") or player_overview.get("position", "")
        opponent = matchups.get(team, "")
        opponent_def = def_stats.get(opponent, {"rush_def": 1.0, "pass_def": 1.0})

        projected_stats = project_player_stats(
            player_overview,
            ppa_entry["avg_ppa"],
            opponent_def,
            position,
        )

        proj_players[athlete_id] = {
            "name": player_overview.get("name") or ppa_entry.get("name", ""),
            "team": team,
            "position": position,
            "opponent": opponent,
            "avg_ppa": ppa_entry["avg_ppa"],
            "stats": projected_stats,
        }

    print(
        f"[cfbd-cfb] projected stats for {len(proj_players)} athletes "
        f"for week={CFBD_WEEK}",
        file=sys.stderr,
    )
    return proj_players


def get_players_map(conn, platform: str) -> Dict[str, int]:
    """
    Map CFBD athlete_id → our internal players.id for a given NCAAF platform.

    Source of truth is ALWAYS payload->>'cfbd_athlete_id' — written by
    sync_cfbd_player_xref.py.  The external_player_id column on yahoo-cfb
    may coincidentally match a CFBD athlete id in some cases, but for
    fantrax-cfb it stores a base-36 re-encoded integer that is a completely
    different number space from CFBD ids, so we NEVER fall back to it here.
    """
    rows = (
        conn.execute(
            text("""
        select id, payload
        from players
        where platform   = :platform
          and sport      = 'NCAAF'
          and payload   ? 'cfbd_athlete_id'
        """),
            {"platform": platform},
        )
        .mappings()
        .all()
    )

    mapping: Dict[str, int] = {}
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        cfbd_id = payload.get("cfbd_athlete_id")
        if cfbd_id:
            mapping[str(cfbd_id)] = r["id"]

    print(
        f"[cfbd-cfb] get_players_map platform={platform}: "
        f"{len(mapping)} players with cfbd_athlete_id",
        file=sys.stderr,
    )
    return mapping


def upsert_projection(
    conn,
    player_id: int,
    source_name: str,
    scoring_format: str,
    projected_points: float,
    stats: Dict[str, Any],
    fetched_at: datetime,
) -> None:
    conn.execute(
        text("""
            insert into projections
              (player_id, source_name, season, scoring_format,
               projected_points, receptions, pass_yd, rush_yd, rec_yd,
               payload, fetched_at, week)
            values
              (:player_id, :source_name, :season, :scoring_format,
               :projected_points, :receptions, :pass_yd, :rush_yd, :rec_yd,
               :payload, :fetched_at, :week)
            """),
        {
            "player_id": player_id,
            "source_name": source_name,
            "season": CFBD_SEASON,
            "scoring_format": scoring_format,
            "week": CFBD_WEEK,
            "projected_points": projected_points,
            "receptions": stats.get("receptions"),
            "pass_yd": stats.get("pass_yd"),
            "rush_yd": stats.get("rush_yd"),
            "rec_yd": stats.get("rec_yd"),
            "payload": json.dumps({"stats": stats, "cfbd_week": CFBD_WEEK}),
            "fetched_at": fetched_at,
        },
    )


def main() -> None:
    fetched_at = now()
    all_written = 0

    with engine.begin() as conn:
        for platform, scoring_format in (
            (YAHOO_CFB, "HALF_PPR"),
            (FANTRAX_CFB, "STD"),
        ):
            players_map = get_players_map(conn, platform)
            source_name = SOURCE_NAMES[platform]

            conn.execute(
                text("""
                    delete from projections
                    where source_name = :source_name
                      and season = :season
                    """),
                {"source_name": source_name, "season": CFBD_SEASON},
            )

            written = 0

            if CFBD_MODE == "actual":
                raw_games = fetch_player_game_stats(CFBD_SEASON, CFBD_WEEK)
                print(
                    f"[cfbd-cfb] fetched {len(raw_games)} games for season={CFBD_SEASON} week={CFBD_WEEK}",
                    file=sys.stderr,
                )
                player_stats = normalize_stats(raw_games)
                print(
                    f"[cfbd-cfb] normalized stats for {len(player_stats)} athletes", file=sys.stderr
                )
            else:
                # projected mode: build defense-adjusted projections
                player_stats = build_projections()

            for athlete_id, stats in player_stats.items():
                player_id = players_map.get(athlete_id)
                if not player_id:
                    continue

                points = score_stats(platform, stats)
                upsert_projection(
                    conn,
                    player_id=player_id,
                    source_name=source_name,
                    scoring_format=scoring_format,
                    projected_points=points,
                    stats=stats,
                    fetched_at=fetched_at,
                )
                written += 1

            all_written += written
            print(
                f"[cfbd-cfb] platform={platform} source_name={source_name}: "
                f"wrote {written} projection rows",
                flush=True,
            )

    # Health check: a fully silent zero is a regression, not a normal empty run.
    # Exit non-zero so Docker restarts the container and logs surface the failure.
    if all_written == 0:
        print(
            "[cfbd-cfb] CRITICAL: 0 projection rows written across all platforms — "
            "check player xref (sync_cfbd_player_xref.py) and CFBD API response.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()