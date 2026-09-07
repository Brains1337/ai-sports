#!/usr/bin/env python3
"""
sync_cfbd_player_xref.py — map CFBD athlete ids onto NCAAF players.

Uses CFBD's /roster endpoint (one bulk call for the whole season) instead of
/player/search (one call per player), to stay well within API budget and
avoid rate limiting. Matches on normalized (name, team) pairs so that players
sharing a name across schools disambiguate correctly when we have a team, and
falls back to name-only matches when there is a unique global match.

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
# Extend this as you find more mismatches in the "zero_matches" log.
TEAM_ALIASES: Dict[str, str] = {
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
    # add more as needed (e.g. "USC": "USC", etc.)
}

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def normalize_name(name: str) -> str:
    """Lowercase, strip non-alphanumerics so similar names match robustly."""
    return "".join(ch.lower() for ch in (name or "") if ch.isalnum())


def normalize_team(team: str) -> str:
    """Normalize team label using TEAM_ALIASES, then strip non-alphanumerics."""
    if not team:
        return ""
    resolved = TEAM_ALIASES.get(team.strip(), team.strip())
    return "".join(ch.lower() for ch in resolved if ch.isalnum())


def canonical_full_name(name: str) -> str:
    """
    Convert 'Last, First' to 'First Last' so Fantrax 'Manning, Arch'
    matches CFBD 'Arch Manning'. Yahoo names already come in 'First Last'.
    """
    if not name:
        return ""
    if "," in name:
        last, first = [part.strip() for part in name.split(",", 1)]
        if first:
            return f"{first} {last}"
    return name


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
    """
    Build (normalized_name, normalized_team) -> [roster_entries].

    Handles both v1 and v2 style keys:
      - first_name / last_name
      - firstName / lastName
      - or combined 'name' field as a fallback
    """
    index: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    for entry in roster:
        if not isinstance(entry, dict):
            continue

        # Name fields vary by version
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
            # Skip entries where we still couldn't infer a name
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
            payload = r["payload"] or {}
            college_team = payload.get("college_team") if isinstance(payload, dict) else None

            # Normalize name so both Yahoo and Fantrax match CFBD roster names
            display_name = canonical_full_name(raw_name)

            name_key = normalize_name(display_name)
            team_key = normalize_team(college_team)

            if not name_key:
                skipped_zero += 1
                continue

            matches = cfbd_index.get((name_key, team_key), [])

            if len(matches) == 0:
                # Fallback: unique name-only match across all teams
                name_only_matches: List[Dict[str, Any]] = []
                for (n_key, _t_key), entries in cfbd_index.items():
                    if n_key == name_key:
                        name_only_matches.extend(entries)
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