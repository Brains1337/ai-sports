#!/usr/bin/env python3
"""
sync_espn_rosters.py — ESPN NFL roster + roster_status_history sync.

Reads current rosters for configured ESPN NFL leagues and writes a
snapshot per player into roster_status_history, aligned with the same
schema used for Yahoo college (platform + league_id + player_id +
roster_status + fantasy_team + position + fetched_at + payload).

This script assumes:

- leagues table has ESPN NFL leagues with:
  - platform = 'espn'
  - sport = 'NFL'
  - external_league_id = ESPN leagueId
- players table has ESPN players from sync_espn.py (platform = 'espn').
"""

import json
import os
from datetime import datetime, timezone

import requests
from sqlalchemy import create_engine, text

SEASON = int(os.getenv("ESPN_SEASON", "2026"))
LEAGUE_IDS = [
    int(value.strip())
    for value in os.getenv(
        "ESPN_LEAGUE_IDS",
        "719429857,209442251",
    ).split(",")
    if value.strip()
]

DATABASE_URL = os.environ["DATABASE_URL"]

BASE = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
COOKIES = {
    key: value
    for key, value in {
        "espn_s2": os.getenv("ESPN_S2"),
        "SWID": os.getenv("ESPN_SWID"),
    }.items()
    if value
}
HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "User-Agent": "fantasy-dashboard/0.1",
}

POSITION_MAP = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "DEF",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def request_json(url: str, params=None) -> dict:
    resp = requests.get(
        url,
        params=params,
        headers=HEADERS,
        cookies=COOKIES,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def get_league_db_id(conn, league_id: int) -> int | None:
    row = (
        conn.execute(
            text("""
            select id
            from leagues
            where external_league_id = :external_league_id
              and platform = 'espn'
              and season = :season
            """),
            {"external_league_id": league_id, "season": SEASON},
        )
        .mappings()
        .first()
    )
    return row["id"] if row else None


def build_team_name(team: dict) -> str:
    """
    Build a human-readable team name from ESPN team object.

    Try multiple common fields in order:
      - team["name"]
      - team["location"] + " " + team["nickname"]
      - team["abbrev"]
      - "Team <id>" fallback
    """
    team_id = team.get("id")

    name = team.get("name")
    if name:
        return str(name).strip()

    loc = team.get("location", "")
    nick = team.get("nickname", "")
    combo = f"{loc} {nick}".strip()
    if combo:
        return combo

    abbrev = team.get("abbrev")
    if abbrev:
        return str(abbrev).strip()

    return f"Team {team_id}" if team_id is not None else "Unknown Team"


def sync_rosters_for_league(conn, league_id: int) -> None:
    league_db_id = get_league_db_id(conn, league_id)
    if league_db_id is None:
        print(f"Skipping league {league_id}: not found in leagues table")
        return

    url = f"{BASE}/segments/0/leagues/{league_id}"
    # Pull both roster and team views so we get team metadata for names
    payload = request_json(
        url,
        params=[("view", "mRoster"), ("view", "mTeam")],
    )

    teams = payload.get("teams", []) or []
    fetched_at = now()
    count = 0

    for team in teams:
        team_id = team.get("id")
        team_name = build_team_name(team)
        team_roster = team.get("roster", {}) or {}
        entries = team_roster.get("entries", []) or []

        for entry in entries:
            player_id = entry.get("playerId")
            roster_pos_id = entry.get("lineupSlotId")
            player_pos_id = (
                entry.get("playerPoolEntry", {})
                .get("player", {})
                .get("defaultPositionId")
            )

            if player_id is None:
                continue

            # Map ESPN position IDs to our pos text
            pos = POSITION_MAP.get(player_pos_id)
            if not pos:
                continue

            # Look up player primary key in players table
            player_row = (
                conn.execute(
                    text("""
                    select id
                    from players
                    where platform = 'espn'
                      and external_player_id = :external_player_id
                    """),
                    {"external_player_id": player_id},
                )
                .mappings()
                .first()
            )
            if not player_row:
                continue

            db_player_id = player_row["id"]

            # For now, treat any rostered player as 'owned'.
            roster_status = "owned"

            history_payload = {
                "espn_league_id": league_id,
                "espn_team_id": team_id,
                "lineup_slot_id": roster_pos_id,
            }

            conn.execute(
                text("""
                    insert into roster_status_history
                    (league_id, player_id, fantasy_team, roster_status, position, fetched_at, payload)
                    values
                    (:league_id, :player_id, :fantasy_team, :roster_status, :position, :fetched_at, cast(:payload as jsonb))
                    """),
                {
                    "league_id": league_db_id,
                    "player_id": db_player_id,
                    "fantasy_team": team_name or None,
                    "roster_status": roster_status,
                    "position": pos,
                    "fetched_at": fetched_at,
                    "payload": json.dumps(history_payload),
                },
            )
            count += 1

    print(
        f"Synced {count} roster entries for ESPN league {league_id} (db id={league_db_id})"
    )


def main() -> None:
    if not COOKIES:
        raise SystemExit("Missing ESPN_S2 and/or ESPN_SWID in environment")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as conn:
        for league_id in LEAGUE_IDS:
            sync_rosters_for_league(conn, league_id)


if __name__ == "__main__":
    main()
