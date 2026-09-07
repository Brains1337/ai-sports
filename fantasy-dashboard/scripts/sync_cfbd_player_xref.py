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

Manual overrides:
  - cfbd_player_overrides table lets you explicitly pin mappings for tricky
    players (e.g., suffix names, nickname mismatches). See notes below.
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

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Position equivalence map for name-only fallback matching.
# This lets K ↔ PK match cleanly without loosening other slots.
POS_EQUIV: Dict[str, set[str]] = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K", "PK"},
    "PK": {"K", "PK"},
    "LB": {"LB"},
    "DB": {"DB"},
    # extend as needed if CFBD ever uses alternates
}


def normalize_name(name: str) -> str:
    """Lowercase and strip non-alphanumerics."""
    return "".join(ch.lower() for ch in (name or "") if ch.isalnum())


def normalize_team(team: str) -> str:
    """Lowercase and strip non-alphanumerics for team labels."""
    if not team:
        return ""
    return "".join(ch.lower() for ch in team if ch.isalnum())


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


def strip_suffix_tokens(full_name: str) -> str:
    """
    Remove trailing generational suffixes like Jr, Sr, II, III, IV.
    'Ben Black III' -> 'Ben Black'
    'John Doe Jr.'  -> 'John Doe'
    """
    if not full_name:
        return ""
    tokens = full_name.replace(".", "").split()
    if tokens and tokens[-1].lower() in {"jr", "sr", "ii", "iii", "iv", "v"}:
        tokens = tokens[:-1]
    return " ".join(tokens)


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
    Applies strip_suffix_tokens so e.g. 'Ben Black III' indexes as 'Ben Black'.
    """
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

        full_name = strip_suffix_tokens(full_name)
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

        for r in rows:
            player_id = r["id"]
            platform = r["platform"]
            raw_name = r["player_name"]
            pos = r["pos"]
            payload = r["payload"] or {}
            college_team = payload.get("college_team") if isinstance(payload, dict) else None

            # Skip DST / team defenses from player mapping
            if pos == "DEF":
                skipped_zero += 1
                continue

            # Manual override hook (for tricky/sleeper cases)
            override = conn.execute(
                text(
                    """
                    select cfbd_athlete_id
                    from cfbd_player_overrides
                    where platform = :platform
                      and player_name = :player_name
                      and (pos is null or pos = :pos)
                    limit 1
                    """
                ),
                {
                    "platform": platform,
                    "player_name": raw_name,
                    "pos": pos,
                },
            ).scalar_one_or_none()

            if override:
                cfbd_id = str(override)
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
                        f"[cfbd-xref] updated {updated} players so far (including overrides)...",
                        flush=True,
                    )
                continue

            # Normalize name and strip suffix tokens for provider side
            display_name = canonical_full_name(raw_name)
            display_name = strip_suffix_tokens(display_name)

            name_key = normalize_name(display_name)
            team_key = normalize_team(college_team)

            if not name_key:
                skipped_zero += 1
                continue

            # 1) Exact (name, team) match
            matches = cfbd_index.get((name_key, team_key), [])

            if len(matches) == 0:
                # 2) Fallback: name-only + position-aware filter
                name_only_matches: List[Dict[str, Any]] = []
                for (n_key, _t_key), entries in cfbd_index.items():
                    if n_key == name_key:
                        name_only_matches.extend(entries)

                if pos and name_only_matches:
                    p = pos.upper()
                    allowed = POS_EQUIV.get(p, {p})
                    name_only_matches = [
                        m
                        for m in name_only_matches
                        if (m.get("position") or "").upper() in allowed
                    ]

                if len(name_only_matches) == 1:
                    matches = name_only_matches
                else:
                    # Either no candidates or still ambiguous (e.g. two RB
                    # Darius Taylor entries at MINN and VT when we don't know
                    # which team Fantrax is using).
                    skipped_zero += 1
                    continue

            if len(matches) > 1:
                # Should be rare now; keep conservative.
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