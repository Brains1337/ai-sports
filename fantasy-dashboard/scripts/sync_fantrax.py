#!/usr/bin/env python3
"""
sync_fantrax.py — Season-long Fantrax college fantasy roster sync.

Iterates over FANTRAX_LEAGUE_IDS, fetches rostered + free-agent players
for each Fantrax CFB league, upserts them into the shared `players` table,
and inserts snapshot rows into `roster_status_history` for season-long
tracking of adds/drops.

Fantrax auth:
  - FANTRAX_API_BASE: documented REST API base
      e.g. https://www.fantrax.com/fxea/general
  - FANTRAX_USER_SECRET_ID: Fantrax userSecretId (from your profile) for
      endpoints like getLeagues/getLeagueInfo.
  - FANTRAX_COOKIE: browser session cookie string for private league endpoints
      that don’t accept userSecretId directly.

Requires: requests, sqlalchemy, psycopg[binary]
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from psycopg import ProgrammingError
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]

FANTRAX_LEAGUE_IDS = os.getenv("FANTRAX_LEAGUE_IDS", "")
FANTRAX_SEASON = int(os.getenv("FANTRAX_SEASON", "2026"))
FANTRAX_PLATFORM = os.getenv("FANTRAX_PLATFORM", "fantrax-cfb")

FANTRAX_API_BASE = os.getenv("FANTRAX_API_BASE", "https://www.fantrax.com/fxea/general")
FANTRAX_USER_SECRET_ID = os.getenv("FANTRAX_USER_SECRET_ID", "")
FANTRAX_COOKIE = os.getenv("FANTRAX_COOKIE", "")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def get_league_ids() -> List[str]:
    raw = FANTRAX_LEAGUE_IDS
    return [lid.strip() for lid in raw.split(",") if lid.strip()]


def make_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if FANTRAX_COOKIE:
        headers["Cookie"] = FANTRAX_COOKIE
    return headers


def fetch_fantrax_players(league_id: str) -> List[Dict[str, Any]]:
    """
    Fetch player+roster data for a single Fantrax league.

    TODO: Implement the real Fantrax integration. This outline assumes:
      - FANTRAX_API_BASE points at the documented REST endpoints.
      - You use FANTRAX_USER_SECRET_ID or FANTRAX_COOKIE to authenticate.

    Typical pattern (you must adapt to your league/game type):

        # League info, teams, player pool, etc.
        info_resp = requests.get(
            f"{FANTRAX_API_BASE}/getLeagueInfo",
            params={"leagueId": league_id},
            headers=make_headers(),
            timeout=30,
        )
        info_resp.raise_for_status()
        info = info_resp.json()

        # Team rosters for a given period (e.g. current scoring period)
        rosters_resp = requests.get(
            f"{FANTRAX_API_BASE}/getTeamRosters",
            params={"leagueId": league_id, "period": 6},
            headers=make_headers(),
            timeout=30,
        )
        rosters_resp.raise_for_status()
        rosters = rosters_resp.json()

        # Map rosters into rows with keys:
        #   name, college_team, position, roster_status, fantasy_team,
        #   note_type, raw_row_text

    For now, this function returns an empty list and logs a message so the
    sync loop is safe to deploy before the HTTP mapping is complete.
    """
    if not FANTRAX_API_BASE:
        print(
            "[fantrax-cfb] FANTRAX_API_BASE not set; cannot fetch players.",
            file=sys.stderr,
        )
        return []

    print(
        f"[fantrax-cfb] fetch_fantrax_players not implemented for leagueId={league_id}; "
        "returning 0 rows.",
        file=sys.stderr,
    )
    return []


def upsert_players_and_history(
    league_external_id: str, rows: List[Dict[str, Any]]
) -> None:
    fetched_at = now()
    with engine.begin() as conn:
        league_row = conn.execute(
            text(
                "select id from leagues "
                "where external_league_id = :lid and platform = :platform"
            ),
            {"lid": league_external_id, "platform": FANTRAX_PLATFORM},
        ).fetchone()

        if league_row is None:
            print(
                f"[fantrax-cfb] No leagues row found for external_league_id={league_external_id} "
                f"platform={FANTRAX_PLATFORM}; not writing history.",
                file=sys.stderr,
            )
            return

        league_id = league_row[0]

        for r in rows:
            try:
                player_row = conn.execute(
                    text("""
                        insert into players (platform, external_player_id, player_name, pos, payload)
                        values (:platform, null, :name, :pos, :payload)
                        on conflict (platform, external_player_id) do nothing
                        returning id
                        """),
                    {
                        "platform": FANTRAX_PLATFORM,
                        "name": r["name"],
                        "pos": r["position"],
                        "payload": json.dumps(
                            {
                                "college_team": r.get("college_team"),
                                "note_type": r.get("note_type"),
                                "raw_row_text": r.get("raw_row_text"),
                            }
                        ),
                    },
                ).fetchone()
            except ProgrammingError as e:
                print(
                    f"[fantrax-cfb] ProgrammingError for player {r.get('name')} "
                    f"{r.get('college_team')} {r.get('position')}: {e}",
                    file=sys.stderr,
                )
                continue

            if player_row is None:
                player_row = conn.execute(
                    text("""
                        select id from players
                        where platform = :platform
                          and player_name = :name
                          and pos = :pos
                        """),
                    {
                        "platform": FANTRAX_PLATFORM,
                        "name": r["name"],
                        "pos": r["position"],
                    },
                ).fetchone()

            if player_row is None:
                continue

            player_id = player_row[0]

            try:
                conn.execute(
                    text("""
                        insert into roster_status_history
                        (league_id, player_id, fantasy_team, roster_status, position, fetched_at, payload)
                        values
                        (:league_id, :player_id, :fantasy_team, :roster_status, :position, :fetched_at, :payload)
                        """),
                    {
                        "league_id": league_id,
                        "player_id": player_id,
                        "fantasy_team": r.get("fantasy_team"),
                        "roster_status": r.get("roster_status", "unknown"),
                        "position": r.get("position"),
                        "fetched_at": fetched_at,
                        "payload": json.dumps({"college_team": r.get("college_team")}),
                    },
                )
            except ProgrammingError as e:
                print(
                    f"[fantrax-cfb] ProgrammingError inserting history for player {r.get('name')}: {e}",
                    file=sys.stderr,
                )
                continue

    print(
        f"[fantrax-cfb] Upserted {len(rows)} rows for league_external_id={league_external_id}, "
        f"snapshot fetched_at={fetched_at.isoformat()}",
        flush=True,
    )


def main() -> None:
    league_ids = get_league_ids()
    if not league_ids:
        print("No FANTRAX_LEAGUE_IDS configured; nothing to sync.", file=sys.stderr)
        return

    for league_id in league_ids:
        print(f"[fantrax-cfb] Syncing league {league_id}", flush=True)
        rows = fetch_fantrax_players(league_id)
        if not rows:
            print(
                f"[fantrax-cfb] 0 rows fetched for leagueId={league_id}; skipping upsert.",
                file=sys.stderr,
            )
            continue
        upsert_players_and_history(league_id, rows)


if __name__ == "__main__":
    main()
