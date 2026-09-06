#!/usr/bin/env python3
"""
sync_fantrax.py — Season-long Fantrax college fantasy roster sync.

Fetches all rostered + free-agent players for a Fantrax CFB league,
upserts them into the shared `players` table, and inserts snapshot rows
into `roster_status_history` for season-long tracking of adds/drops.

This script intentionally leaves the HTTP/API call to Fantrax as a TODO,
since league endpoints and auth vary by setup. Implement fetch_fantrax_players()
to return rows shaped like the Yahoo sync:

{
    "name": str,
    "college_team": str,
    "position": str,
    "roster_status": "owned" | "free_agent" | "waivers" | "unknown",
    "fantasy_team": Optional[str],
    "note_type": str,
    "raw_row_text": str,
}

Requires: requests, sqlalchemy, psycopg[binary]
"""

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]

FANTRAX_LEAGUE_ID = os.getenv("FANTRAX_LEAGUE_ID", "2hbybmp6msnsbuqa")
FANTRAX_SEASON = int(os.getenv("FANTRAX_SEASON", "2026"))
FANTRAX_PLATFORM = os.getenv("FANTRAX_PLATFORM", "fantrax-cfb")

# These will depend on your Fantrax setup; wire them to your secrets/CI.
FANTRAX_API_BASE = os.getenv("FANTRAX_API_BASE", "")
FANTRAX_API_TOKEN = os.getenv("FANTRAX_API_TOKEN", "")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_fantrax_players() -> List[Dict[str, Any]]:
    """
    TODO: Implement Fantrax API integration here.

    This function should return a list of dicts with keys:
    name, college_team, position, roster_status, fantasy_team, note_type, raw_row_text.

    Example outline (you must adapt to real endpoints/JSON):

        url = f"{FANTRAX_API_BASE}/league/{FANTRAX_LEAGUE_ID}/players"
        headers = {"Authorization": f"Bearer {FANTRAX_API_TOKEN}"}
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # map data into the expected row format
    """
    if not FANTRAX_API_BASE or not FANTRAX_API_TOKEN:
        print(
            "FANTRAX_API_BASE and FANTRAX_API_TOKEN must be set to sync Fantrax.",
            file=sys.stderr,
        )
        return []

    # Placeholder implementation; replace with real mapping.
    # Returning an empty list prevents accidental writes with bogus data.
    return []


def upsert_players_and_history(rows: List[Dict[str, Any]]) -> None:
    fetched_at = now()
    with engine.begin() as conn:
        league_row = conn.execute(
            text(
                "select id from leagues "
                "where external_league_id = :lid and platform = :platform"
            ),
            {"lid": FANTRAX_LEAGUE_ID, "platform": FANTRAX_PLATFORM},
        ).fetchone()

        if league_row is None:
            print(
                f"No leagues row found for external_league_id={FANTRAX_LEAGUE_ID} "
                f"platform={FANTRAX_PLATFORM}; not writing history.",
                file=sys.stderr,
            )
            return

        league_id = league_row[0]

        for r in rows:
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
                    "payload": {
                        "college_team": r.get("college_team"),
                        "note_type": r.get("note_type"),
                        "raw_row_text": r.get("raw_row_text"),
                    },
                },
            ).fetchone()

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
                    "payload": {"college_team": r.get("college_team")},
                },
            )

    print(
        f"Upserted {len(rows)} rows for Fantrax CFB league_id={league_id}, "
        f"snapshot fetched_at={fetched_at.isoformat()}",
        flush=True,
    )


def main() -> None:
    rows = fetch_fantrax_players()
    if not rows:
        print("No Fantrax rows fetched; nothing to upsert.", file=sys.stderr)
        return
    upsert_players_and_history(rows)


if __name__ == "__main__":
    main()
