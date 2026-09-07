#!/usr/bin/env python3
"""
sync_cfbd_player_xref.py — map CFBD athlete ids onto NCAAF players.

Uses CFBD's /roster endpoint (one bulk call for the whole season) instead of
/player/search (one call per player), to stay well within API budget and
avoid rate limiting. Matches on normalized (name, team) pairs so that players
sharing a name across schools (e.g. two "Austin Simmons") disambiguate
correctly using the college_team we already scraped from Yahoo/Fantrax.
"""

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
CFBD_API_KEY = os.environ["CFBD_API_KEY"]
CFBD_SEASON = int(os.getenv("CFBD_SEASON", "2026"))
CFBD_MAX_RETRIES = int(os.getenv("CFBD_MAX_RETRIES", "5"))
CFBD_RETRY_BASE_SLEEP = float(os.getenv("CFBD_RETRY_BASE_SLEEP", "2.0"))

CFBD_BASE = "https://api.collegefootballdata.com"
HEADERS = {
    "Authorization": f"Bearer {CFBD_API_KEY}",
    "Accept": "application/json",
}

# Map common Yahoo/Fantrax team abbreviations -> CFBD team name.
# Extend this as you find more mismatches in the "unmatched" log.
TEAM_ALIASES = {
    "ND": "Notre Dame",
    "VT": "Virginia Tech",
    "BC": "Boston College",
    "NW": "Northwestern",
    "LOU": "Louisville",
    "UVA": "Virginia",
    "MINN": "Minnesota",
    "PITT": "Pittsburgh",
    "MSST": "Mississippi State",
    "TENN": "Tennessee",
    # ... add more as needed
}

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def normalize_name(name: str) -> str:
    return "".join(ch.lower() for ch in (name or "") if ch.isalnum())


def normalize_team(team: str) -> str:
    if not team:
        return ""
    resolved = TEAM_ALIASES.get(team.strip(), team.strip())
    return "".join(ch.lower() for ch in resolved if ch.isalnum())


def fetch_full_roster(season: int) -> List[Dict[str, Any]]:
    """One bulk call for every FBS team's roster for the season."""
    for attempt in range(1, CFBD_MAX_RETRIES + 1):
        resp = requests.get(
            f"{CFBD_BASE}/roster",
            params={"year": season},
            headers=HEADERS,
            timeout=60,
        )
        if resp.status_code in (429, 502, 503, 504):
            sleep_for = CFBD_RETRY_BASE_SLEEP * attempt
            print(
                f"[cfbd-xref] roster fetch got {resp.status_code}, "
                f"retrying in {sleep_for:.1f}s (attempt {attempt})",
                file=sys.stderr,
            )
            time.sleep(sleep_for)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Failed to fetch CFBD roster after retries")


def build_cfbd_index(
    roster: List[Dict[str, Any]]
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """(normalized_name, normalized_team) -> list of CFBD roster entries."""
    index: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for entry in roster:
        first = entry.get("first_name") or ""
        last = entry.get("last_name") or ""
        full_name = f"{first} {last}".strip()
        team = entry.get("team") or ""
        key = (normalize_name(full_name), normalize_team(team))
        index.setdefault(key, []).append(entry)
    return index


def main() -> None:
    print(f"[cfbd-xref] fetching full CFBD roster for season={CFBD_SEASON}...", flush=True)
    roster = fetch_full_roster(CFBD_SEASON)
    print(f"[cfbd-xref] fetched {len(roster)} CFBD roster entries (1 API call)", flush=True)

    cfbd_index = build_cfbd_index(roster)

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
                    """
                )
            )
            .mappings()
            .all()
        )

    print(f"[cfbd-xref] loaded {len(rows)} NCAAF players needing cfbd_athlete_id", flush=True)

    updated = 0
    skipped_zero = 0
    skipped_multi = 0

    with engine.begin() as conn:
        for r in rows:
            player_id = r["id"]
            platform = r["platform"]
            name = r["player_name"]
            payload = r["payload"] or {}
            college_team = payload.get("college_team") if isinstance(payload, dict) else None

            key = (normalize_name(name), normalize_team(college_team))
            matches = cfbd_index.get(key, [])

            if len(matches) == 0:
                # Fallback: name-only match, only accept if unique across ALL teams
                name_only_matches = [
                    v for k, v in cfbd_index.items() if k[0] == key[0]
                ]
                flat = [m for group in name_only_matches for m in group]
                if len(flat) == 1:
                    matches = flat
                else:
                    skipped_zero += 1
                    continue

            if len(matches) > 1:
                skipped_multi += 1
                continue

            cfbd_id = str(matches[0].get("id"))

            conn.execute(
                text(
                    """
                    update players
                    set payload = jsonb_set(
                        coalesce(payload, '{}'::jsonb),
                        '{cfbd_athlete_id}',
                        to_jsonb(cast(:cfbd_id as text)),
                        true
                    )
                    where id = :id
                    """
                ),
                {"cfbd_id": cfbd_id, "id": player_id},
            )
            updated += 1

            if updated % 500 == 0:
                print(f"[cfbd-xref] updated {updated} players so far...", flush=True)

    print(
        f"[cfbd-xref] done: updated={updated}, zero_matches={skipped_zero}, "
        f"multi_matches={skipped_multi}, total_processed={len(rows)}",
        flush=True,
    )


if __name__ == "__main__":
    main()