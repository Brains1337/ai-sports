#!/usr/bin/env python3
"""
sync_cfbd_player_reference.py — pull all CFBD player info into cfbd_player_reference.

Uses the fewest possible CFBD API calls to stay within a 30k/month Tier 2 budget.
Two requests per season total:

  1. /roster?year=<season>&classification=fbs  — all FBS roster players with
     name, team, position, physicals (height, weight, jersey), hometown,
     and recruit IDs. (~15k-18k rows in a single call)
  2. /teams/fbs?year=<season> — team metadata per player: conference,
     division, classification, school name, abbreviation, team_id, etc.

Every CFBD field is stored in its own column — no payload catch-all — so
downstream code queries directly without JSON parsing.

recruit_ids is stored as a comma-separated text string rather than text[].

Uses psycopg directly (not SQLAlchemy text()) so Python types are properly
adapted to PostgreSQL types without manual casting.

Idempotent: each (athlete_id, season) row is upserted. The cfbd_sync_runs
table records how many API calls were consumed.

Usage:
  CFBD_API_KEY=... python sync_cfbd_player_reference.py
  CFBD_API_KEY=... CFBD_SEASON=2025 python sync_cfbd_player_reference.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg
import requests

CFBD_API_KEY = os.environ["CFBD_API_KEY"]
CFBD_SEASON = int(os.getenv("CFBD_SEASON", "2026"))
DATABASE_URL = os.environ["DATABASE_URL"]

CFBD_BASE = "https://api.collegefootballdata.com"
HEADERS = {
    "Authorization": f"Bearer {CFBD_API_KEY}",
    "Accept": "application/json",
}

INSERT_SQL = """
    INSERT INTO cfbd_player_reference (
        athlete_id, first_name, last_name, full_name,
        position, team, height, weight, jersey,
        home_city, home_state, home_country,
        home_latitude, home_longitude, home_county_fips,
        recruit_ids, team_id, conference, division,
        classification, abbreviation, school, season
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s)
    ON CONFLICT (athlete_id, season) DO UPDATE SET
        first_name        = excluded.first_name,
        last_name         = excluded.last_name,
        full_name         = excluded.full_name,
        position          = excluded.position,
        team              = excluded.team,
        height            = excluded.height,
        weight            = excluded.weight,
        jersey            = excluded.jersey,
        home_city         = excluded.home_city,
        home_state        = excluded.home_state,
        home_country      = excluded.home_country,
        home_latitude     = excluded.home_latitude,
        home_longitude    = excluded.home_longitude,
        home_county_fips  = excluded.home_county_fips,
        recruit_ids       = excluded.recruit_ids,
        team_id           = excluded.team_id,
        conference        = excluded.conference,
        division          = excluded.division,
        classification    = excluded.classification,
        abbreviation      = excluded.abbreviation,
        school            = excluded.school,
        fetched_at        = now()
"""

SYNC_RUNS_SQL = """
    INSERT INTO cfbd_sync_runs
        (season, endpoint, row_count, call_count, status, meta)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (season, endpoint) DO UPDATE SET
        fetched_at  = now(),
        row_count   = excluded.row_count,
        call_count  = excluded.call_count,
        status      = excluded.status,
        error_text  = excluded.error_text,
        meta        = excluded.meta
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def cfbd_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET a CFBD endpoint with retry + rate-limit handling."""
    max_retries = 5
    base_delay = 2
    url = f"{CFBD_BASE}{path}"

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=120)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", str(base_delay * attempt)))
                print(
                    f"[cfbd-ref] rate limited (429) on attempt {attempt}/{max_retries}, "
                    f"waiting {retry_after}s",
                    file=sys.stderr,
                )
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"[cfbd-ref] request failed (attempt {attempt}/{max_retries}), "
                    f"retrying in {delay}s: {exc}",
                    file=sys.stderr,
                )
                time.sleep(delay)
            else:
                raise

    raise RuntimeError("Unexpected: retries exhausted without exception")


def fetch_roster(season: int) -> List[Dict[str, Any]]:
    """Single call: all FBS roster players for the season."""
    data = cfbd_get(
        "/roster",
        {"year": season, "classification": "fbs"},
    )
    print(
        f"[cfbd-ref] fetched {len(data)} roster entries (1 API call, "
        f"season={season})",
        file=sys.stderr,
    )
    return data


def fetch_teams(season: int) -> Dict[str, Dict[str, Any]]:
    """Single call: team-level metadata."""
    data = cfbd_get(
        "/teams/fbs",
        {"year": season},
    )
    team_index: Dict[str, Dict[str, Any]] = {}
    for team in data:
        school = team.get("school") or team.get("name", "")
        if school:
            team_index[school] = team

    print(
        f"[cfbd-ref] fetched {len(data)} team entries (1 API call, season={season})",
        file=sys.stderr,
    )
    return team_index


def format_recruit_ids(recruit_ids: List[str]) -> Optional[str]:
    """Format recruit IDs as a comma-separated text string."""
    if not recruit_ids:
        return None
    return ",".join(recruit_ids)


def build_rows(roster: List[Dict[str, Any]], teams: Dict[str, Dict[str, Any]]) -> List[tuple]:
    """Convert CFBD roster data into database insert tuples."""
    rows: List[tuple] = []
    for player in roster:
        athlete_id = str(player.get("id") or "")
        if not athlete_id:
            continue

        first_name = player.get("firstName")
        last_name = player.get("lastName")
        full_name = f"{first_name or ''} {last_name or ''}".strip()

        team = player.get("team") or ""
        team_info = teams.get(team, {}) if team else {}

        raw_recruit_ids = player.get("recruitIds") or []
        if isinstance(raw_recruit_ids, list):
            recruit_ids_str = [str(rid) for rid in raw_recruit_ids if rid is not None]
        else:
            recruit_ids_str = []
        recruit_ids_text = format_recruit_ids(recruit_ids_str)

        rows.append((
            athlete_id,
            first_name,
            last_name,
            full_name,
            player.get("position"),
            team,
            player.get("height"),
            player.get("weight"),
            player.get("jersey"),
            player.get("homeCity"),
            player.get("homeState"),
            player.get("homeCountry"),
            player.get("homeLatitude"),
            player.get("homeLongitude"),
            player.get("homeCountyFIPS"),
            recruit_ids_text,
            team_info.get("id"),
            team_info.get("conference"),
            team_info.get("division"),
            team_info.get("classification"),
            team_info.get("abbreviation"),
            team_info.get("school"),
            CFBD_SEASON,
        ))

    return rows


def main() -> None:
    fetched_at = now()
    roster = fetch_roster(CFBD_SEASON)
    teams = fetch_teams(CFBD_SEASON)

    rows = build_rows(roster, teams)
    print(f"[cfbd-ref] upserting {len(rows)} rows...", flush=True)

    conn = psycopg.connect(DATABASE_URL)
    try:
        # Record sync run
        conn.execute(SYNC_RUNS_SQL, (
            CFBD_SEASON,
            "roster+teams",
            len(roster),
            2,
            "ok",
            json.dumps({"teams_fetched": len(teams)}),
        ))

        # Batch upsert — psycopg3 executemany is on a cursor
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, rows)
        conn.commit()
        written = len(rows)

    except psycopg.Error as exc:
        conn.rollback()
        print(f"[cfbd-ref] DB ERROR: {exc}", file=sys.stderr)
        # Try inserting one row to get the exact error
        if rows:
            try:
                conn.execute(INSERT_SQL, rows[0])
                conn.commit()
            except psycopg.Error as exc2:
                conn.rollback()
                print(f"[cfbd-ref] SINGLE ROW ERROR: {exc2}", file=sys.stderr)
        raise
    finally:
        conn.close()

    print(
        f"[cfbd-ref] DONE: season={CFBD_SEASON}, rows_written={written}, "
        f"api_calls=2",
        flush=True,
    )


if __name__ == "__main__":
    main()
