#!/usr/bin/env python3
"""sync_cfbd_player_reference.py — populate the cfbd_player_reference table.

This script pulls CFBD roster + team data and writes ONLY to the
cfbd_player_reference table. It does NOT write into players.payload
and does NOT modify players.

Two CFBD API calls per season (stays well within Tier 2's 30k/month):
  1. /roster  — full FBS roster (RosterPlayer objects)
  2. /teams/fbs — team metadata for enriching roster entries

The cfbd_player_reference table is the single source of truth for CFBD
athletes.  Downstream scripts (sync_yahoo_members.py, sync_fantrax.py)
look up athlete_id by matching on (normalized_name, normalized_team)
and use that athlete_id to populate roster_assignments.

Usage:
  CFBD_API_KEY=... DATABASE_URL=... python sync_cfbd_player_reference.py
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
                f"[cfbd-ref] roster fetch got {resp.status_code}, "
                f"retrying in {sleep_for:.1f}s (attempt {attempt})",
                file=sys.stderr,
            )
            time.sleep(sleep_for)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Failed to fetch CFBD roster after retries")


def fetch_teams_fbs(season: int) -> List[Dict[str, Any]]:
    """Second API call: FBS team metadata for enrichment."""
    for attempt in range(1, CFBD_MAX_RETRIES + 1):
        resp = requests.get(
            f"{CFBD_BASE}/teams/fbs",
            params={"year": season},
            headers=HEADERS,
            timeout=60,
        )
        if resp.status_code in (429, 502, 503, 504):
            sleep_for = CFBD_RETRY_BASE_SLEEP * attempt
            print(
                f"[cfbd-ref] teams/fbs fetch got {resp.status_code}, "
                f"retrying in {sleep_for:.1f}s (attempt {attempt})",
                file=sys.stderr,
            )
            time.sleep(sleep_for)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Failed to fetch CFBD teams/fbs after retries")


def build_cfbd_index(
    roster: List[Dict[str, Any]],
) -> Tuple[
    Dict[Tuple[str, str], List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]
]:
    """
    Build two indexes from CFBD roster data:
    1. (normalized_name, normalized_team) -> [roster_entries]
    2. athlete_id -> [roster_entries]  (for enrichment with team metadata)

    Handles both v1 and v2 style keys:
      - first_name / last_name
      - firstName / lastName
      - or combined 'name' field as a fallback

    Applies strip_suffix_tokens so e.g. 'Ben Black III' indexes as 'Ben Black'.
    """
    name_team_index: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    athlete_index: Dict[str, List[Dict[str, Any]]] = {}
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

        athlete_id = str(entry.get("id") or entry.get("athlete_id") or "")
        if athlete_id:
            athlete_index.setdefault(athlete_id, []).append(entry)

        if name_key:
            key = (name_key, team_key)
            name_team_index.setdefault(key, []).append(entry)

    return name_team_index, athlete_index


def build_team_metadata_index(
    teams: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    Build team_name -> team_metadata from /teams/fbs.
    Used to enrich cfbd_player_reference with conference, division, etc.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for t in teams:
        if not isinstance(t, dict):
            continue
        # /teams/fbs returns 'name' (e.g. "Michigan") or 'school' / 'abbreviation'
        team_key_candidates = [
            t.get("name"),
            t.get("school"),
            t.get("abbreviation"),
        ]
        for key in team_key_candidates:
            if key:
                norm_key = normalize_team(key)
                if norm_key:
                    index[norm_key] = t
                break
    return index


def _parse_height(height_raw: Any) -> Any:
    """
    Parse CFBD height into numeric(5,2) feet.inches format.
      '6-2'  -> 6.2
      '74'   -> 6.2  (74 inches = 6'2")
    Returns None if unparseable.
    """
    if height_raw is None:
        return None
    if isinstance(height_raw, (int, float)):
        # Assume inches
        feet = int(height_raw) // 12
        inches = int(height_raw) % 12
        return float(f"{feet}.{inches}")
    if isinstance(height_raw, str):
        if "-" in height_raw:
            parts = height_raw.split("-")
            if len(parts) == 2:
                feet = int(parts[0])
                inches = int(parts[1])
                return float(f"{feet}.{inches}")
        # Try pure integer string
        try:
            total = int(height_raw)
            feet = total // 12
            inches = total % 12
            return float(f"{feet}.{inches}")
        except (ValueError, TypeError):
            pass
    return None


def upsert_cfbd_players(conn, roster: List[Dict[str, Any]], season: int) -> int:
    """
    Upsert all CFBD roster entries into cfbd_player_reference.
    Returns the number of rows upserted.

    Populates normalized_name and normalized_team columns for downstream
    matching by roster sync scripts.
    """
    upserted = 0

    for entry in roster:
        if not isinstance(entry, dict):
            continue

        athlete_id = str(entry.get("id") or entry.get("athlete_id") or "")
        if not athlete_id:
            continue

        first = entry.get("first_name") or entry.get("firstName") or ""
        last = entry.get("last_name") or entry.get("lastName") or ""
        if first or last:
            full_name = f"{first} {last}".strip()
        else:
            full_name = (entry.get("name") or "").strip()
        full_name = strip_suffix_tokens(full_name)

        team = entry.get("team") or ""

        # Parse height from "6-2" or 74 (inches) format
        height_raw = entry.get("height") or entry.get("heightString")
        height_val = _parse_height(height_raw)

        # Recruit IDs: CFBD returns a list; we store as comma-separated text
        recruit_ids_raw = entry.get("recruitIds") or entry.get("recruit_ids")
        if isinstance(recruit_ids_raw, list):
            recruit_ids = ",".join(str(rid) for rid in recruit_ids_raw if rid)
        elif recruit_ids_raw:
            recruit_ids = str(recruit_ids_raw)
        else:
            recruit_ids = None

        normalized_name = normalize_name(full_name)
        normalized_team = normalize_team(team)

        conn.execute(
            text("""
                insert into cfbd_player_reference (
                    athlete_id, season, first_name, last_name, full_name,
                    position, team, height, weight, jersey,
                    home_city, home_state, home_country,
                    home_latitude, home_longitude, home_county_fips,
                    recruit_ids, fetched_at,
                    normalized_name, normalized_team
                ) values (
                    :athlete_id, :season, :first_name, :last_name, :full_name,
                    :position, :team, :height, :weight, :jersey,
                    :home_city, :home_state, :home_country,
                    :home_latitude, :home_longitude, :home_county_fips,
                    :recruit_ids, now(),
                    :normalized_name, :normalized_team
                )
                on conflict (athlete_id, season) do update set
                    first_name       = excluded.first_name,
                    last_name        = excluded.last_name,
                    full_name        = excluded.full_name,
                    position         = excluded.position,
                    team             = excluded.team,
                    height           = excluded.height,
                    weight           = excluded.weight,
                    jersey           = excluded.jersey,
                    home_city        = excluded.home_city,
                    home_state       = excluded.home_state,
                    home_country     = excluded.home_country,
                    home_latitude    = excluded.home_latitude,
                    home_longitude   = excluded.home_longitude,
                    home_county_fips = excluded.home_county_fips,
                    recruit_ids      = excluded.recruit_ids,
                    fetched_at       = now(),
                    normalized_name  = excluded.normalized_name,
                    normalized_team  = excluded.normalized_team
            """),
            {
                "athlete_id": athlete_id,
                "season": season,
                "first_name": first or None,
                "last_name": last or None,
                "full_name": full_name or None,
                "position": entry.get("position") or None,
                "team": team or None,
                "height": height_val,
                "weight": entry.get("weight"),
                "jersey": entry.get("jersey"),
                "home_city": entry.get("homeCity") or entry.get("home_city"),
                "home_state": entry.get("homeState") or entry.get("home_state"),
                "home_country": entry.get("homeCountry") or entry.get("home_country"),
                "home_latitude": entry.get("homeLatitude")
                or entry.get("home_latitude"),
                "home_longitude": entry.get("homeLongitude")
                or entry.get("home_longitude"),
                "home_county_fips": entry.get("homeCountyFIPS")
                or entry.get("home_county_fips"),
                "recruit_ids": recruit_ids,
                "normalized_name": normalized_name or None,
                "normalized_team": normalized_team or None,
            },
        )
        upserted += 1

        if upserted % 2000 == 0:
            print(
                f"[cfbd-ref] upserted {upserted} cfbd_player_reference rows...",
                flush=True,
            )

    return upserted


def enrich_with_team_metadata(
    conn, team_index: Dict[str, Dict[str, Any]], season: int
) -> int:
    """
    Enrich cfbd_player_reference rows with /teams/fbs metadata
    (team_id, conference, division, classification, abbreviation, school)
    using team name as the join key.
    """
    if not team_index:
        return 0

    updated = 0
    rows = (
        conn.execute(
            text("""
            select athlete_id, team
            from cfbd_player_reference
            where season = :season
              and team is not null
              and team_id is null
            """),
            {"season": season},
        )
        .mappings()
        .all()
    )

    for r in rows:
        team_norm = normalize_team(r["team"])
        meta = team_index.get(team_norm)
        if not meta:
            continue

        conn.execute(
            text("""
                update cfbd_player_reference
                set team_id = :team_id,
                    conference = :conference,
                    division = :division,
                    classification = :classification,
                    abbreviation = :abbreviation,
                    school = :school
                where athlete_id = :athlete_id and season = :season
                """),
            {
                "team_id": meta.get("id"),
                "conference": meta.get("conference"),
                "division": meta.get("division"),
                "classification": (
                    meta.get("classification") or ("fbs" if meta.get("fbs") else None)
                ),
                "abbreviation": meta.get("abbreviation"),
                "school": meta.get("school") or meta.get("name"),
                "athlete_id": r["athlete_id"],
                "season": season,
            },
        )
        updated += 1

    return updated


def main() -> None:
    print(
        f"[cfbd-ref] fetching CFBD roster for season={CFBD_SEASON}...",
        flush=True,
    )
    roster = fetch_full_roster(CFBD_SEASON)
    print(
        f"[cfbd-ref] fetched {len(roster)} CFBD roster entries (1 API call)",
        flush=True,
    )

    if roster:
        sample = roster[0]
        print(
            f"[cfbd-ref] sample roster keys={list(sample.keys())}",
            file=sys.stderr,
        )

    print(
        f"[cfbd-ref] fetching FBS teams for season={CFBD_SEASON}...",
        flush=True,
    )
    teams_fbs = fetch_teams_fbs(CFBD_SEASON)
    print(
        f"[cfbd-ref] fetched {len(teams_fbs)} FBS teams (2nd API call)",
        flush=True,
    )

    # Build indexes
    name_team_index, athlete_index = build_cfbd_index(roster)
    team_index = build_team_metadata_index(teams_fbs)

    with engine.begin() as conn:
        # 1) Upsert all CFBD players into cfbd_player_reference
        upserted = upsert_cfbd_players(conn, roster, CFBD_SEASON)
        print(
            f"[cfbd-ref] upserted {upserted} rows into cfbd_player_reference",
            flush=True,
        )

        # 2) Enrich with team metadata from /teams/fbs
        enriched = enrich_with_team_metadata(conn, team_index, CFBD_SEASON)
        print(
            f"[cfbd-ref] enriched {enriched} cfbd_player_reference rows with team metadata",
            flush=True,
        )

        # 3) Show unmatched players (for diagnostics only)
        total = conn.execute(
            text("select count(*) from cfbd_player_reference where season = :season"),
            {"season": CFBD_SEASON},
        ).scalar_one()
        print(
            f"[cfbd-ref] total cfbd_player_reference rows for season={CFBD_SEASON}: {total}",
            flush=True,
        )


if __name__ == "__main__":
    main()
