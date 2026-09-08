#!/usr/bin/env python3
"""
sync_cfbd_cfb_projections.py — CollegeFootballData → projections (NCAAF).

Pulls player game/season stats from CollegeFootballData (CFBD) and writes
per-league-scored fantasy projections into the shared `projections` table,
using each league's exact scoring rules (Yahoo EDIT vs Fantrax New Freshman).

Env:
  DATABASE_URL      → PostgreSQL connection string.
  CFBD_API_KEY      → CollegeFootballData API key (Bearer token).
  CFBD_SEASON       → season year, default 2026.
  CFBD_WEEK         → week to project for (int).

Writes to `projections` with:
  source_name in ('cfbd_cfb_proj_yahoo', 'cfbd_cfb_proj_fantrax')
  season, scoring_format, projected_points, plus raw stat breakdown in payload.
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from sqlalchemy import create_engine, text

from cfb_scoring import YAHOO_CFB, FANTRAX_CFB, score_stats

DATABASE_URL = os.environ["DATABASE_URL"]
CFBD_API_KEY = os.environ["CFBD_API_KEY"]
CFBD_SEASON = int(os.getenv("CFBD_SEASON", "2026"))
CFBD_WEEK = int(os.getenv("CFBD_WEEK", "1"))

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

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


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
               payload, fetched_at)
            values
              (:player_id, :source_name, :season, :scoring_format,
               :projected_points, :receptions, :pass_yd, :rush_yd, :rec_yd,
               :payload, :fetched_at)
            """),
        {
            "player_id": player_id,
            "source_name": source_name,
            "season": CFBD_SEASON,
            "scoring_format": scoring_format,
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
    raw_games = fetch_player_game_stats(CFBD_SEASON, CFBD_WEEK)
    print(
        f"[cfbd-cfb] fetched {len(raw_games)} games for season={CFBD_SEASON} week={CFBD_WEEK}",
        file=sys.stderr,
    )

    player_stats = normalize_stats(raw_games)
    print(
        f"[cfbd-cfb] normalized stats for {len(player_stats)} athletes", file=sys.stderr
    )

    fetched_at = now()

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

            print(
                f"[cfbd-cfb] platform={platform} source_name={source_name}: "
                f"wrote {written} projection rows",
                flush=True,
            )


if __name__ == "__main__":
    main()