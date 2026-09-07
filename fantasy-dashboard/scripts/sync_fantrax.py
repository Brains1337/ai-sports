#!/usr/bin/env python3
"""
sync_fantrax.py — Season-long Fantrax college fantasy roster sync.

Adds a global player directory from Fantrax getAdp so we can map
Fantrax player IDs to real names/teams/positions before writing into
our unified players table.

Endpoints used:
  - getAdp?sport=...     → player info + ADP, filtered by sport.
  - getLeagueInfo        → league metadata, playerInfo (eligibility), teamInfo, etc.
  - getTeamRosters       → per-team rosters for a given period.

Env:
  DATABASE_URL            → PostgreSQL connection string.
  FANTRAX_API_BASE        → e.g. https://www.fantrax.com/fxea/general
  FANTRAX_USER_SECRET_ID  → (not used here directly, but kept for future).
  FANTRAX_COOKIE          → browser session cookie string for private leagues.
  FANTRAX_LEAGUE_IDS      → comma-separated Fantrax league IDs.
  FANTRAX_SEASON          → season year (int, default 2026).
  FANTRAX_PLATFORM        → platform key for players/leagues (default "fantrax-cfb").
  FANTRAX_SPORT           → sport code for getAdp, e.g. "CFB" or "NCAA_FB".
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

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

# For CFB this may need to be "CFB" or "NCAA_FB" depending on Fantrax;
# keep it configurable via env so you can adjust without code changes.
FANTRAX_SPORT = os.getenv("FANTRAX_SPORT", "CFB")

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


# ──────────────────────────────────────────────────────────────────────────────
# Global player directory via getAdp
# ──────────────────────────────────────────────────────────────────────────────

def fetch_player_directory() -> Dict[str, Dict[str, Any]]:
    """
    Build a global directory of Fantrax player IDs to name/team/pos
    using the documented getAdp endpoint.

    Returns:
      { "<fantrax_id>": {"name": str, "team": str|None, "position": str|None,
                         "raw": original_player_object}, ... }
    """
    directory: Dict[str, Dict[str, Any]] = {}

    if not FANTRAX_API_BASE:
        print(
            "[fantrax-cfb] FANTRAX_API_BASE not set; cannot fetch player directory.",
            file=sys.stderr,
        )
        return directory

    headers = make_headers()

    try:
        resp = requests.get(
            f"{FANTRAX_API_BASE}/getAdp",
            params={"sport": FANTRAX_SPORT},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(
            f"[fantrax-cfb] getAdp failed for sport={FANTRAX_SPORT}: {e}",
            file=sys.stderr,
        )
        return directory

    # Handle both object and array shapes
    if isinstance(data, dict):
        print(
            f"[fantrax-cfb] getAdp sport={FANTRAX_SPORT} top-level keys={list(data.keys())}",
            file=sys.stderr,
        )
        players = (
            data.get("players")
            or data.get("rows")
            or data.get("adp")
            or data.get("list")
            or []
        )
    elif isinstance(data, list):
        print(
            f"[fantrax-cfb] getAdp sport={FANTRAX_SPORT} returned list with {len(data)} entries",
            file=sys.stderr,
        )
        players = data
    else:
        print(
            f"[fantrax-cfb] getAdp sport={FANTRAX_SPORT} unexpected type={type(data)}",
            file=sys.stderr,
        )
        players = []

    print(
        f"[fantrax-cfb] getAdp player array size={len(players)}",
        file=sys.stderr,
    )

    if players:
        sample = players[0]
        try:
            print(
                "[fantrax-cfb] sample getAdp player entry:",
                json.dumps(sample, indent=2)[:1000],
                file=sys.stderr,
            )
        except Exception:
            print(
                "[fantrax-cfb] sample getAdp player entry (non-JSON serializable)",
                file=sys.stderr,
            )

    for p in players:
        if not isinstance(p, dict):
            continue

        pid = p.get("id") or p.get("playerId") or p.get("playerID")
        if not pid:
            continue
        pid_str = str(pid)

        name = (
            p.get("name")
            or p.get("playerName")
            or p.get("fullName")
            or p.get("displayName")
        )

        team = (
            p.get("team")
            or p.get("proTeam")
            or p.get("proTeamAbbrev")
            or p.get("collegeTeam")
        )

        pos = None
        elig = p.get("eligiblePos") or p.get("positions")
        if isinstance(elig, list) and elig:
            pos = str(elig[0])
        elif isinstance(elig, str):
            pos = elig
        else:
            pos = p.get("position") or p.get("pos")

        if pos == "DST":
            pos = "DEF"

        directory[pid_str] = {
            "name": name,
            "team": team,
            "position": pos,
            "raw": p,
        }

    print(
        f"[fantrax-cfb] player directory size={len(directory)} from getAdp",
        file=sys.stderr,
    )
    return directory


# ──────────────────────────────────────────────────────────────────────────────
# League + roster sync, enriched by directory
# ──────────────────────────────────────────────────────────────────────────────

def fetch_fantrax_players(
    league_id: str,
    player_directory: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Fantrax CFB league sync (full pool + rosters):

      - getLeagueInfo → teamInfo (team names/IDs), rosterPeriods, playerInfo (eligibility).
      - getTeamRosters → rosters (per-team entries with rosterItems).
      - player_directory (from getAdp) → global ID → name/team/pos mapping.

    Returns a list of rows with keys:
      name, external_id, college_team, position, roster_status, fantasy_team, note_type, raw_row_text.
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
            params={"leagueId": league_id},
            headers=headers,
            timeout=60,
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

    raw_player_info = info.get("playerInfo") or {}
    raw_team_info = info.get("teamInfo") or []

    # TEMP diagnostics around playerInfo — we already know it's limited, but keep one sample.
    if isinstance(raw_player_info, dict) and raw_player_info:
        first_key = next(iter(raw_player_info.keys()))
        try:
            print(
                "[fantrax-cfb] sample playerInfo entry:",
                json.dumps(raw_player_info[first_key], indent=2)[:500],
                file=sys.stderr,
            )
        except Exception:
            print(
                "[fantrax-cfb] sample playerInfo entry (non-JSON serializable)",
                file=sys.stderr,
            )

    # teamInfo: build teamId → name map
    team_names: Dict[str, str] = {}
    for team in raw_team_info:
        if not isinstance(team, dict):
            continue
        t_id = team.get("teamId") or team.get("id")
        name = team.get("teamName") or team.get("name")
        if t_id and name:
            team_names[str(t_id)] = name

    # playerInfo: eligibility/status, but not names for this league
    player_pool: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_player_info, dict):
        for pid, pdata in raw_player_info.items():
            if not isinstance(pdata, dict):
                continue
            pid_str = str(pid)

            pos = None
            elig = pdata.get("eligiblePos")
            if isinstance(elig, list) and elig:
                pos = str(elig[0])
            elif isinstance(elig, str):
                pos = elig
            else:
                pos = pdata.get("position") or pdata.get("pos")

            if pos == "DST":
                pos = "DEF"

            player_pool[pid_str] = {
                "eligible_pos": pos,
                "status": pdata.get("status"),
                "raw": pdata,
            }

    print(
        f"[fantrax-cfb] playerInfo pool size={len(player_pool)}",
        file=sys.stderr,
    )

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
            timeout=60,
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

    # ── 3) Map roster entries (rostered players) ───────────────────────────────
    rostered_ids: Set[str] = set()

    for team_entry in team_entries:
        if not isinstance(team_entry, dict):
            continue

        team_id = team_entry.get("teamId") or team_entry.get("id")
        fantasy_team = (
            team_names.get(str(team_id))
            or team_entry.get("teamName")
            or team_entry.get("name")
        )

        # Fantrax CFB rosters use "rosterItems".
        player_list = (
            team_entry.get("rosterItems")
            or team_entry.get("players")
            or team_entry.get("lineup")
            or []
        )

        for player in player_list:
            if not isinstance(player, dict):
                continue

            player_id = player.get("id")
            pos = player.get("position")
            status = player.get("status")

            if not player_id or not pos:
                continue

            pid_str = str(player_id)
            rostered_ids.add(pid_str)

            # Merge in directory info first, then league-level eligibility.
            dir_meta = player_directory.get(pid_str, {})
            pool_meta = player_pool.get(pid_str, {})

            name = dir_meta.get("name") or f"Player {pid_str}"
            college_team = dir_meta.get("team") or None

            pooled_pos = pool_meta.get("eligible_pos")
            directory_pos = dir_meta.get("position")

            if directory_pos:
                pos = directory_pos
            elif pooled_pos:
                pos = pooled_pos

            if pos == "DST":
                pos = "DEF"

            if status == "ACTIVE":
                roster_status = "owned"
            else:
                roster_status = "bench"

            rows.append(
                {
                    "name": name,
                    "external_id": pid_str,
                    "college_team": college_team,
                    "position": pos,
                    "roster_status": roster_status,
                    "fantasy_team": fantasy_team,
                    "note_type": "",
                    "raw_row_text": json.dumps(
                        {
                            "roster_item": player,
                            "pool": pool_meta.get("raw"),
                            "directory": dir_meta.get("raw"),
                        },
                        separators=(",", ":"),
                    ),
                }
            )

    # ── 4) Add remaining pool/directory players as free agents ────────────────
    # Use the union of IDs from player_pool and player_directory so we pick up
    # names even for players not currently rostered in the league.
    # IMPORTANT: If a player only exists in player_pool (league pool) and NOT
    # in getAdp (directory), and is not rostered, we SKIP it to avoid inserting
    # "Player 05xxx" placeholder rows.
    all_ids: Set[str] = set(player_pool.keys()) | set(player_directory.keys())

    for pid_str in all_ids:
        if pid_str in rostered_ids:
            continue

        dir_meta = player_directory.get(pid_str)
        pool_meta = player_pool.get(pid_str, {})

        # If this player is only in the league pool (playerInfo) and never
        # appears in getAdp (no directory entry) and is not rostered,
        # skip it to avoid placeholder rows.
        if dir_meta is None and pid_str in player_pool:
            continue

        if dir_meta is None:
            dir_meta = {}

        name = dir_meta.get("name") or f"Player {pid_str}"
        college_team = dir_meta.get("team")

        pos = dir_meta.get("position") or pool_meta.get("eligible_pos")
        if pos == "DST":
            pos = "DEF"

        rows.append(
            {
                "name": name,
                "external_id": pid_str,
                "college_team": college_team,
                "position": pos,
                "roster_status": "free_agent",
                "fantasy_team": None,
                "note_type": "",
                "raw_row_text": json.dumps(
                    {
                        "pool": pool_meta.get("raw"),
                        "directory": dir_meta.get("raw"),
                    },
                    separators=(",", ":"),
                ),
            }
        )

    print(
        f"[fantrax-cfb] fetch_fantrax_players leagueId={league_id} period={period}: "
        f"{len(rows)} players (rostered + free agents)",
        file=sys.stderr,
    )
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# DB upsert
# ──────────────────────────────────────────────────────────────────────────────

def upsert_players_and_history(
    league_external_id: str, rows: List[Dict[str, Any]]
) -> None:
    fetched_at = now()
    with engine.begin() as conn:
        # Attach history to the first fantrax-cfb league row.
        league_row = conn.execute(
            text(
                "select id from leagues "
                "where platform = :platform "
                "order by id limit 1"
            ),
            {"platform": FANTRAX_PLATFORM},
        ).fetchone()

        if league_row is None:
            print(
                f"[fantrax-cfb] No leagues row found for platform={FANTRAX_PLATFORM}; not writing history.",
                file=sys.stderr,
            )
            return

        league_id = league_row[0]

        for r in rows:
            # Convert Fantrax string ID to a stable numeric value for external_player_id.
            try:
                ext_numeric = int(str(r.get("external_id", "") or ""), 36)
            except ValueError:
                ext_numeric = None

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
                        "external_player_id": ext_numeric,
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
                        "external_player_id": ext_numeric,
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


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    league_ids = get_league_ids()
    if not league_ids:
        print("No FANTRAX_LEAGUE_IDS configured; nothing to sync.", file=sys.stderr)
        return

    # Fetch the global directory once per run, reuse for all leagues.
    player_directory = fetch_player_directory()

    for league_id in league_ids:
        print(f"[fantrax-cfb] Syncing league {league_id}", flush=True)
        rows = fetch_fantrax_players(league_id, player_directory)
        if not rows:
            print(
                f"[fantrax-cfb] 0 rows fetched for leagueId={league_id}; skipping upsert.",
                file=sys.stderr,
            )
            continue
        upsert_players_and_history(league_id, rows)


if __name__ == "__main__":
    main()