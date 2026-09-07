#!/usr/bin/env python3
"""
sync_cfbd_player_xref.py — map CFBD athlete ids onto NCAAF players.

Uses CFBD's /roster endpoint (one bulk call for the whole season) instead of
/player/search (one call per player), to stay well within API budget and
avoid rate limiting. Matches on normalized (name, team) pairs so that players
sharing a name across schools disambiguate correctly when we have a team, and
falls back to name-only+position matches when possible.

Supported platforms:
  - yahoo-cfb   (names already 'First Last', college_team from Yahoo scraper)
  - fantrax-cfb (names like 'Last, First'; converted to 'First Last' here)
"""

import os
import sys
import time
from typing import Any, Dict, List, Tuple

import requests
from sqlalchemy import create_engine, text

from teams_normalizer import TEAM_CODE_TO_NAME  # optional for future use

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

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def normalize_name(name: str) -> str:
    return "".join(ch.lower() for ch in (name or "") if ch.isalnum())


def normalize_team(team: str) -> str:
    if not team:
        return ""
    return "".join(ch.lower() for ch in team if ch.isalnum())


def canonical_full_name(name: str) -> str:
    if not name:
        return ""
    if "," in name:
        last, first = [part.strip() for part in name.split(",", 1)]
        if first:
            return f"{first} {last}"
    return name


def fetch_full_roster(season: int) -> List[Dict[str, Any]]:
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
    roster: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    index: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    for entry in roster:
        if not isinstance(entry, dict):
            continue

        first = entry.get("first_name") or entry.get("firstName") or ""
        last = entry.get("last_name") or entry.get("lastName") or ""

        if first or last:
            full_name = f"{first} {last}".strip()
        else:
            full_name = (entry.get("name") or "").strip()

        team = entry.get("team") or ""
        name_key = normalize_name(full_name)
        team_key = normalize_team(team)
        if not name_key:
            continue

        key = (name_key, team_key)
        index.setdefault(key, []).append(entry)

    return index


def main() -> None:
    print(
        f"[cfbd-xref] fetching full CFBD roster for season={CFBD_SEASON}...",
        flush=True,
    )
    roster = fetch_full_roster(CFBD_SEASON)
    print(
        f"[cfbd-xref] fetched {len(roster)} CFBD roster entries (1 API call)",
        flush=True,
    )

    if roster:
        sample = roster[0]
        print(
            f"[cfbd-xref] sample roster keys={list(sample.keys())}",
            file=sys.stderr,
        )

    cfbd_index = build_cfbd_index(roster)

    with engine.begin() as conn:
        rows = conn.execute(text("""
                    select id, platform, player_name, pos, payload
                    from players
                    where sport = 'NCAAF'
                      and platform in ('yahoo-cfb', 'fantrax-cfb')
                      and (payload->>'cfbd_athlete_id') is null
                    order by id
                    """)).mappings().all()

    print(
        f"[cfbd-xref] loaded {len(rows)} NCAAF players needing cfbd_athlete_id",
        flush=True,
    )

    updated = 0
    skipped_zero = 0
    skipped_multi = 0

    with engine.begin() as conn:
        for r in rows:
            player_id = r["id"]
            platform = r["platform"]
            raw_name = r["player_name"]
            pos = r["pos"]
            payload = r["payload"] or {}
            college_team = (
                payload.get("college_team") if isinstance(payload, dict) else None
            )

            # Skip DST / team defenses from player mapping
            if pos == "DEF":
                skipped_zero += 1
                continue

            display_name = canonical_full_name(raw_name)
            name_key = normalize_name(display_name)
            team_key = normalize_team(college_team)

            if not name_key:
                skipped_zero += 1
                continue

            matches = cfbd_index.get((name_key, team_key), [])

            if len(matches) == 0:
                # Fallback: name-only + position filter
                name_only_matches: List[Dict[str, Any]] = []
                for (n_key, _t_key), entries in cfbd_index.items():
                    if n_key == name_key:
                        name_only_matches.extend(entries)

                if pos and name_only_matches:
                    # Simple position-based filter: first letter match
                    p0 = pos[0].upper()
                    name_only_matches = [
                        m
                        for m in name_only_matches
                        if (m.get("position") or "").upper().startswith(p0)
                    ]

                if len(name_only_matches) == 1:
                    matches = name_only_matches
                else:
                    skipped_zero += 1
                    continue

            if len(matches) > 1:
                skipped_multi += 1
                continue

            cfbd_id = str(matches[0].get("id"))

            conn.execute(
                text("""
                    update players
                    set payload = jsonb_set(
                        coalesce(payload, '{}'::jsonb),
                        '{cfbd_athlete_id}',
                        to_jsonb(cast(:cfbd_id as text)),
                        true
                    )
                    where id = :id
                    """),
                {"cfbd_id": cfbd_id, "id": player_id},
            )
            updated += 1

            if updated % 500 == 0:
                print(
                    f"[cfbd-xref] updated {updated} players so far...",
                    flush=True,
                )

    print(
        "[cfbd-xref] done: "
        f"updated={updated}, zero_matches={skipped_zero}, "
        f"multi_matches={skipped_multi}, total_processed={len(rows)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
