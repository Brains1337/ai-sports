#!/usr/bin/env python3
"""
sync_cfbd_player_xref.py — map CFBD athlete ids onto NCAAF players.

For each NCAAF player in our DB (Yahoo CFB + Fantrax CFB) that does not yet
have a cfbd_athlete_id in payload, call CollegeFootballData's /player/search
endpoint and, when there is a unique, exact name match, store that CFBD id in
players.payload->'cfbd_athlete_id'.

After running this, sync_cfbd_cfb_projections.py can join CFBD game stats to
our players via payload->'cfbd_athlete_id'.
"""

import json
import os
import sys
import time
from typing import Any, Dict, List

import requests
from sqlalchemy import create_engine, text

# Env:
#   DATABASE_URL   → PostgreSQL connection string (same as other scripts).
#   CFBD_API_KEY   → CollegeFootballData API key (Bearer token).
#   CFBD_SEASON    → season year, default 2026.
#   CFBD_MAX_PLAYERS     → optional cap on players processed per run (default 5000).
#   CFBD_SLEEP_SECONDS   → optional sleep between CFBD calls (default 0.1s).

DATABASE_URL = os.environ["DATABASE_URL"]
CFBD_API_KEY = os.environ["CFBD_API_KEY"]
CFBD_SEASON = int(os.getenv("CFBD_SEASON", "2026"))
CFBD_MAX_PLAYERS = int(os.getenv("CFBD_MAX_PLAYERS", "5000"))
CFBD_SLEEP_SECONDS = float(os.getenv("CFBD_SLEEP_SECONDS", "0.1"))

CFBD_BASE = "https://api.collegefootballdata.com"
HEADERS = {
    "Authorization": f"Bearer {CFBD_API_KEY}",
    "Accept": "application/json",
}

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def normalize_name(name: str) -> str:
    """Lowercase, strip non-alphanumerics so 'Micah Gilbert' == 'MICAH  GILBERT'."""
    return "".join(ch.lower() for ch in (name or "") if ch.isalnum())


def search_cfbd_player(name: str) -> List[Dict[str, Any]]:
    """
    Hit CFBD /player/search for a player name in a given season.

    Docs: GET /player/search?searchTerm=...&year=...
    Returns objects with fields including id, name, team, position.[web:100]
    """
    resp = requests.get(
        f"{CFBD_BASE}/player/search",
        params={"searchTerm": name, "year": CFBD_SEASON},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()  # list of players


def main() -> None:
    # 1) Load NCAAF players needing a CFBD id
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    select id, platform, player_name, pos, payload
                    from players
                    where sport = 'NCAAF'
                      and platform in ('yahoo-cfb', 'fantrax-cfb')
                      and (payload->>'cfbd_athlete_id') is null
                    order by id
                    limit :max_players
                    """
                ),
                {"max_players": CFBD_MAX_PLAYERS},
            )
            .mappings()
            .all()
        )

    print(
        f"[cfbd-xref] loaded {len(rows)} NCAAF players needing cfbd_athlete_id",
        flush=True,
    )

    updated = 0
    skipped_zero = 0
    skipped_multi = 0
    errors = 0

    # 2) For each player, search CFBD and, on a unique exact name match, write cfbd_athlete_id
    with engine.begin() as conn:
        for idx, r in enumerate(rows, start=1):
            player_id = r["id"]
            platform = r["platform"]
            name = r["player_name"]
            norm_target = normalize_name(name)

            try:
                cfbd_players = search_cfbd_player(name)
            except Exception as e:
                print(
                    f"[cfbd-xref] error searching CFBD for {name!r} "
                    f"({platform}, id={player_id}): {e}",
                    file=sys.stderr,
                )
                errors += 1
                continue

            exact_matches = [
                p
                for p in cfbd_players
                if normalize_name(p.get("name")) == norm_target
            ]

            if len(exact_matches) == 0:
                skipped_zero += 1
                continue

            if len(exact_matches) > 1:
                # Too ambiguous to trust automatically; log sample and skip
                sample = ", ".join(
                    f"{m.get('id')}:{m.get('name')}@{m.get('team')}"
                    for m in exact_matches[:3]
                )
                print(
                    f"[cfbd-xref] multiple CFBD matches for {name!r} "
                    f"({platform}, id={player_id}); sample={sample}",
                    file=sys.stderr,
                )
                skipped_multi += 1
                continue

            match = exact_matches[0]
            cfbd_id = str(match.get("id"))
            team = match.get("team")

            conn.execute(
                text(
                    """
                    update players
                    set payload = jsonb_set(
                        coalesce(payload, '{}'::jsonb),
                        '{cfbd_athlete_id}',
                        to_jsonb(:cfbd_id::text),
                        true
                    )
                    where id = :id
                    """
                ),
                {"cfbd_id": cfbd_id, "id": player_id},
            )

            updated += 1
            if updated % 100 == 0:
                print(
                    f"[cfbd-xref] updated {updated} players "
                    f"(processed {idx} total so far)...",
                    flush=True,
                )

            time.sleep(CFBD_SLEEP_SECONDS)

    print(
        "[cfbd-xref] done: "
        f"updated={updated}, zero_matches={skipped_zero}, "
        f"multi_matches={skipped_multi}, errors={errors}",
        flush=True,
    )


if __name__ == "__main__":
    main()