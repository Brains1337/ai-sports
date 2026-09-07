#!/usr/bin/env python3
"""
sync_fantrax.py — Season-long Fantrax college fantasy roster sync.

Iterates over FANTRAX_LEAGUE_IDS, fetches rostered players
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

FANTRAX_API_BASE = os.getenv(
    "FANTRAX_API_BASE", "https://www.fantrax.com/fxea/general"
)
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
    First-pass Fantrax CFB roster sync:
      - getLeagueInfo → teamInfo (team names + IDs)
      - getTeamRosters → rosters (per-team entries with rosterItems)
      - returns one row per rostered player (ACTIVE or RESERVE)
    """

    if not FANTRAX_API_BASE:
        print(
            "[fantrax-cfb] FANTRAX_API_BASE not set; cannot fetch players.",
            file=sys.stderr,
        )
        return []

    headers = make_headers()
    rows: List[Dict[str, Any]] = []

    # ── 1) League info ─────────────────────────────────────────────────────────
    try:
        info_resp = requests.get(
            f"{FANTRAX_API_BASE}/getLeagueInfo",
            params={"leagueId": league_id, "excludePlayerInfo": "true"},
            headers=headers,
            timeout=30,
        )
        info_resp.raise_for_status()
        info = info_resp.json()
    except Exception as e:
        print(
            f"[fantrax-cfb] getLeagueInfo failed for leagueId={league_id}: {e}",
            file=sys.stderr,
        )
        return []

    print(
        f"[fantrax-cfb] getLeagueInfo leagueId={league_id} keys={list(info.keys())}",
        file=sys.stderr,
    )

    # teamInfo is present per your logs; build teamId → name map
    team_names: Dict[str, str] = {}
    raw_team_info = info.get("teamInfo") or []
    for team in raw_team_info:
        if not isinstance(team, dict):
            continue
        team_id = team.get("teamId") or team.get("id")
        name = team.get("teamName") or team.get("name")
        if team_id and name:
            team_names[str(team_id)] = name

    # Pick a roster period (last one in rosterPeriods / rosterInfo.rosterPeriods)
    period = 1
    roster_periods = (
        info.get("rosterInfo", {}).get("rosterPeriods")
        or info.get("rosterPeriods")
        or []
    )
    if isinstance(roster_periods, list) and roster_periods:
        last = roster_periods[-1]
        if isinstance(last, dict):
            period = last.get("number", period)

    # ── 2) Team rosters ────────────────────────────────────────────────────────
    try:
        rosters_resp = requests.get(
            f"{FANTRAX_API_BASE}/getTeamRosters",
            params={"leagueId": league_id, "period": period},
            headers=headers,
            timeout=30,
        )
        rosters_resp.raise_for_status()
        rosters = rosters_resp.json()
    except Exception as e:
        print(
            f"[fantrax-cfb] getTeamRosters failed for leagueId={league_id}, period={period}: {e}",
            file=sys.stderr,
        )
        return []

    print(
        f"[fantrax-cfb] getTeamRosters leagueId={league_id} keys={list(rosters.keys())}",
        file=sys.stderr,
    )

    raw_rosters = rosters.get("rosters") or {}
    if isinstance(raw_rosters, dict):
        team_entries = list(raw_rosters.values())
    else:
        team_entries = raw_rosters if isinstance(raw_rosters, list) else []

    print(
        f"[fantrax-cfb] getTeamRosters leagueId={league_id} period={period}: "
        f"{len(team_entries)} roster entries",
        file=sys.stderr,
    )

    if team_entries:
        sample = team_entries[0]
        try:
            print(
                "[fantrax-cfb] sample roster entry:",
                json.dumps(sample, indent=2)[:1000],
                file=sys.stderr,
            )
        except Exception:
            print(
                "[fantrax-cfb] sample roster entry (non-JSON serializable)",
                file=sys.stderr,
            )

    # ── 3) Map roster entries to player rows ───────────────────────────────────
    for team_entry in team_entries:
        if not isinstance(team_entry, dict):
            continue

        team_id = team_entry.get("teamId") or team_entry.get("id")
        fantasy_team = (
            team_names.get(str(team_id))
            or team_entry.get("teamName")
            or team_entry.get("name")
        )

        # Fantrax CFB rosters use "rosterItems" per your sample.
        player_list = (
            team_entry.get("rosterItems")
            or team_entry.get("players")
            or team_entry.get("lineup")
            or []
        )

        for player in player_list:
            if not isinstance(player, dict):
                continue

            # rosterItems: id, position, status
            player_id = player.get("id")
            pos = player.get("position")
            status = player.get("status")

            if not player_id or not pos:
                continue

            # First pass: use Fantrax ID as a stand‑in name
            name = f"Player {player_id}"

            college_team = None  # not exposed here; can be enriched later

            # Map Fantrax status → our roster_status
            if status == "ACTIVE":
                roster_status = "owned"
            else:
                roster_status = "bench"

            rows.append(
                {
                    "name": name,
                    "external_id": str(player_id),
                    "college_team": college_team,
                    "position": pos,
                    "roster_status": roster_status,
                    "fantasy_team": fantasy_team,
                    "note_type": "",
                    "raw_row_text": json.dumps(player, separators=(",", ":")),
                }
            )

    print(
        f"[fantrax-cfb] fetch_fantrax_players leagueId={league_id} period={period}: "
        f"{len(rows)} rostered players",
        file=sys.stderr,
    )
    return rows


def upsert_players_and_history(
    league_external_id: str, rows: List[Dict[str, Any]]
) -> None:
    fetched_at = now()
    with engine.begin() as conn:
        league_row = conn.execute(
            text(
                "select id from leagues "
                "where platform = :platform "
                "order by id limit 1"
            ),
            {"platform": FANTRAX_PLATFORM},
)       .fetchone()

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
                    text(
                        """
                        insert into players (platform, external_player_id, player_name, pos, payload)
                        values (:platform, :external_player_id, :player_name, :pos, :payload)
                        on conflict (platform, external_player_id) do update set
                          player_name = excluded.player_name,
                          pos = excluded.pos,
                          payload = excluded.payload
                        returning id
                        """
                    ),
                    {
                        "platform": FANTRAX_PLATFORM,
                        "external_player_id": r["external_id"],
                        "player_name": r["name"],
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
                # Fallback lookup if INSERT returned no row
                player_row = conn.execute(
                    text(
                        """
                        select id from players
                        where platform = :platform
                          and external_player_id = :external_player_id
                        """
                    ),
                    {
                        "platform": FANTRAX_PLATFORM,
                        "external_player_id": r["external_id"],
                    },
                ).fetchone()

            if player_row is None:
                continue

            player_id = player_row[0]

            try:
                conn.execute(
                    text(
                        """
                        insert into roster_status_history
                        (league_id, player_id, fantasy_team, roster_status, position, fetched_at, payload)
                        values
                        (:league_id, :player_id, :fantasy_team, :roster_status, :position, :fetched_at, :payload)
                        """
                    ),
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