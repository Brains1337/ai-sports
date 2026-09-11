#!/usr/bin/env python3
"""
sync_fantrax.py — Season-long Fantrax college fantasy roster sync.

Player directory is built from two Fantrax endpoints:
  - getPlayerIds  → full player list WITH college team (Team field)
  - getAdp        → ADP enrichment (supplements getPlayerIds)

Then per-league roster sync:
  - getLeagueInfo → teamInfo, rosterPeriods, playerInfo (eligibility/status)
  - getTeamRosters → per-team rosters (rostered players)

All three sets (rostered, pool-only from playerInfo, directory-only) are
written to players + roster_status_history.

Env:
  DATABASE_URL            → PostgreSQL connection string.
  FANTRAX_API_BASE        → e.g. https://www.fantrax.com/fxea/general
  FANTRAX_COOKIE          → browser session cookie string for private leagues.
  FANTRAX_LEAGUE_IDS      → comma-separated Fantrax league IDs.
  FANTRAX_SEASON          → season year (int, default 2026).
  FANTRAX_PLATFORM        → platform key in players/leagues (default "fantrax-cfb").
  FANTRAX_SPORT           → sport code passed to getPlayerIds + getAdp.
                            Use "NCAAF" for college football (Go SDK constant).
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

import requests
from sqlalchemy.exc import IntegrityError, DataError
from sqlalchemy import create_engine, text

from teams_normalizer import get_def_team

DATABASE_URL = os.environ["DATABASE_URL"]

FANTRAX_LEAGUE_IDS = os.getenv("FANTRAX_LEAGUE_IDS", "")
FANTRAX_SEASON = int(os.getenv("FANTRAX_SEASON", "2026"))
FANTRAX_PLATFORM = os.getenv("FANTRAX_PLATFORM", "fantrax-cfb")
FANTRAX_API_BASE = os.getenv(
    "FANTRAX_API_BASE", "https://www.fantrax.com/fxea/general"
)
FANTRAX_COOKIE = os.getenv("FANTRAX_COOKIE", "")

# Use "NCAAF" to match the Fantrax Go SDK sport constant — this is the correct
# value for getPlayerIds and getAdp for college football.
# Previously we used "CFB" which only worked for getAdp.
FANTRAX_SPORT = os.getenv("FANTRAX_SPORT", "NCAAF")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def get_league_ids() -> List[str]:
    return [lid.strip() for lid in FANTRAX_LEAGUE_IDS.split(",") if lid.strip()]


def make_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if FANTRAX_COOKIE:
        headers["Cookie"] = FANTRAX_COOKIE
    return headers


# ──────────────────────────────────────────────────────────────────────────────
# Global player directory (getPlayerIds + getAdp)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_player_directory() -> Dict[str, Dict[str, Any]]:
    """
    Build a global directory: Fantrax player ID → {name, team, position, adp, ...}

    Step 1: getPlayerIds — returns Player objects WITH Team field (college team).
      Go type: map[string]Player where Player has Name, FantraxId, Team, Position,
      StatsIncId, RotowireId, SportRadarId.

    Step 2: getAdp — supplements with ADP_PPR values. Only backfills team/pos
      when getPlayerIds didn't provide them (edge cases only).
    """
    directory: Dict[str, Dict[str, Any]] = {}
    headers = make_headers()

    # ── Step 1: getPlayerIds ───────────────────────────────────────────────────
    try:
        resp = requests.get(
            f"{FANTRAX_API_BASE}/getPlayerIds",
            params={"sport": FANTRAX_SPORT},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        pid_data = resp.json()
    except Exception as e:
        print(
            f"[fantrax-cfb] getPlayerIds failed for sport={FANTRAX_SPORT}: {e}",
            file=sys.stderr,
        )
        pid_data = {}

    if isinstance(pid_data, dict):
        print(
            f"[fantrax-cfb] getPlayerIds sport={FANTRAX_SPORT}: "
            f"dict with {len(pid_data)} entries",
            file=sys.stderr,
        )
        if pid_data:
            first_key = next(iter(pid_data))
            try:
                print(
                    "[fantrax-cfb] sample getPlayerIds entry:",
                    json.dumps(pid_data[first_key], indent=2)[:500],
                    file=sys.stderr,
                )
            except Exception:
                pass

        for pid_str, p in pid_data.items():
            if not isinstance(p, dict):
                continue
            pos = p.get("position")
            if pos == "DST":
                pos = "DEF"
            directory[str(pid_str)] = {
                "name": p.get("name"),
                "team": p.get("team"),        # college team — present in Player struct
                "position": pos,
                "rotowire_id": p.get("rotowireId"),
                "stats_inc_id": p.get("statsIncId"),
                "sportradar_id": p.get("sportRadarId"),
                "raw": p,
            }

    elif isinstance(pid_data, list):
        print(
            f"[fantrax-cfb] getPlayerIds sport={FANTRAX_SPORT}: "
            f"list with {len(pid_data)} entries",
            file=sys.stderr,
        )
        if pid_data:
            try:
                print(
                    "[fantrax-cfb] sample getPlayerIds entry:",
                    json.dumps(pid_data[0], indent=2)[:500],
                    file=sys.stderr,
                )
            except Exception:
                pass

        for p in pid_data:
            if not isinstance(p, dict):
                continue
            pid_str = str(
                p.get("fantraxId") or p.get("id") or p.get("playerID") or ""
            )
            if not pid_str:
                continue
            pos = p.get("position")
            if pos == "DST":
                pos = "DEF"
            directory[pid_str] = {
                "name": p.get("name"),
                "team": p.get("team"),
                "position": pos,
                "rotowire_id": p.get("rotowireId"),
                "stats_inc_id": p.get("statsIncId"),
                "sportradar_id": p.get("sportRadarId"),
                "raw": p,
            }
    else:
        print(
            f"[fantrax-cfb] getPlayerIds unexpected type={type(pid_data)}",
            file=sys.stderr,
        )

    print(
        f"[fantrax-cfb] getPlayerIds directory size={len(directory)}",
        file=sys.stderr,
    )

    # ── Step 2: getAdp — enrich with ADP; backfill team/pos if missing ────────
    try:
        resp = requests.get(
            f"{FANTRAX_API_BASE}/getAdp",
            params={"sport": FANTRAX_SPORT},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        adp_data = resp.json()
    except Exception as e:
        print(
            f"[fantrax-cfb] getAdp failed (non-fatal, ADP enrichment skipped): {e}",
            file=sys.stderr,
        )
        adp_data = []

    if isinstance(adp_data, list):
        adp_list = adp_data
    elif isinstance(adp_data, dict):
        adp_list = (
            adp_data.get("players")
            or adp_data.get("rows")
            or adp_data.get("adp")
            or adp_data.get("list")
            or []
        )
    else:
        adp_list = []

    adp_enriched = 0
    adp_new = 0
    for p in adp_list:
        if not isinstance(p, dict):
            continue
        pid_str = str(p.get("id") or p.get("playerId") or p.get("playerID") or "")
        if not pid_str:
            continue

        adp_val = p.get("ADP_PPR") or p.get("ADP")

        if pid_str in directory:
            directory[pid_str]["adp"] = adp_val
            # Only backfill if getPlayerIds left these blank
            if not directory[pid_str].get("team"):
                directory[pid_str]["team"] = p.get("team") or p.get("proTeam")
            if not directory[pid_str].get("position"):
                pos = p.get("pos") or p.get("position")
                if pos == "DST":
                    pos = "DEF"
                directory[pid_str]["position"] = pos
            adp_enriched += 1
        else:
            # Only in getAdp — add as a fallback entry
            pos = p.get("pos") or p.get("position")
            if pos == "DST":
                pos = "DEF"
            directory[pid_str] = {
                "name": p.get("name"),
                "team": p.get("team") or p.get("proTeam"),
                "position": pos,
                "adp": adp_val,
                "raw": p,
            }
            adp_new += 1

    print(
        f"[fantrax-cfb] getAdp: enriched={adp_enriched} existing, "
        f"added={adp_new} new entries",
        file=sys.stderr,
    )
    print(
        f"[fantrax-cfb] final directory size={len(directory)} "
        f"(getPlayerIds + getAdp)",
        file=sys.stderr,
    )
    return directory


# ──────────────────────────────────────────────────────────────────────────────
# League + roster sync
# ──────────────────────────────────────────────────────────────────────────────


def fetch_fantrax_players(
    league_id: str,
    player_directory: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Full player pool sync for one league:
      - Rostered players (from getTeamRosters)
      - Free agents (from getLeagueInfo playerInfo pool + directory remainder)

    Returns list of row dicts with keys:
      name, external_id, college_team, def_team, position, roster_status,
      fantasy_team, note_type, raw_row_text
    """
    if not FANTRAX_API_BASE:
        print(
            "[fantrax-cfb] FANTRAX_API_BASE not set; skipping.",
            file=sys.stderr,
        )
        return []

    headers = make_headers()
    rows: List[Dict[str, Any]] = []

    # ── 1) getLeagueInfo ──────────────────────────────────────────────────────
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
        f"[fantrax-cfb] getLeagueInfo leagueId={league_id} "
        f"keys={list(info.keys())}",
        file=sys.stderr,
    )

    raw_player_info = info.get("playerInfo") or {}
    raw_team_info = info.get("teamInfo") or []

    if isinstance(raw_player_info, dict) and raw_player_info:
        first_key = next(iter(raw_player_info.keys()))
        try:
            print(
                "[fantrax-cfb] sample playerInfo entry:",
                json.dumps(raw_player_info[first_key], indent=2)[:500],
                file=sys.stderr,
            )
        except Exception:
            pass

    # teamId → fantasy team name
    team_names: Dict[str, str] = {}
    for team in raw_team_info:
        if not isinstance(team, dict):
            continue
        t_id = team.get("teamId") or team.get("id")
        name = team.get("teamName") or team.get("name")
        if t_id and name:
            team_names[str(t_id)] = name

    # playerInfo: eligibility / waiver status for the league pool
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

    # Pick roster period
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

    # ── 2) getTeamRosters ─────────────────────────────────────────────────────
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
            f"[fantrax-cfb] getTeamRosters failed leagueId={league_id} "
            f"period={period}: {e}",
            file=sys.stderr,
        )
        return []

    print(
        f"[fantrax-cfb] getTeamRosters leagueId={league_id} "
        f"keys={list(rosters.keys())}",
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
        try:
            print(
                "[fantrax-cfb] sample roster entry:",
                json.dumps(team_entries[0], indent=2)[:1000],
                file=sys.stderr,
            )
        except Exception:
            pass

    # ── 3) Map rostered players ───────────────────────────────────────────────
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

            dir_meta = player_directory.get(pid_str, {})
            pool_meta = player_pool.get(pid_str, {})

            # Prefer getPlayerIds name/team/pos over roster item
            name = dir_meta.get("name") or f"Player {pid_str}"
            college_team = dir_meta.get("team") or None

            if dir_meta.get("position"):
                pos = dir_meta["position"]
            elif pool_meta.get("eligible_pos"):
                pos = pool_meta["eligible_pos"]

            if pos == "DST":
                pos = "DEF"

            is_def = pos == "DEF"
            def_team = get_def_team(college_team, name) if is_def else None

            # Fantrax status "ACTIVE" = starting lineup; everything else on a
            # roster is benched but still "owned". Free agents are handled in
            # section 4 below.  "bench" is a valid lineup_status value, NOT
            # a roster_status value — the CHECK constraint only allows
            # 'owned', 'waivers', 'free_agent' for roster_status.
            is_starter = status == "ACTIVE"
            roster_status = "owned"
            lineup_status = "starter" if is_starter else "bench"

            rows.append(
                {
                    "name": name,
                    "external_id": pid_str,
                    "college_team": college_team,
                    "def_team": def_team,
                    "position": pos,
                    "roster_status": roster_status,
                    "lineup_status": lineup_status,
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

    # ── 4) Free agents: pool + directory remainder ────────────────────────────
    all_ids: Set[str] = set(player_pool.keys()) | set(player_directory.keys())

    for pid_str in all_ids:
        if pid_str in rostered_ids:
            continue

        dir_meta = player_directory.get(pid_str) or {}
        pool_meta = player_pool.get(pid_str, {})

        name = dir_meta.get("name") or f"Player {pid_str}"
        college_team = dir_meta.get("team")

        pos = dir_meta.get("position") or pool_meta.get("eligible_pos")
        if pos == "DST":
            pos = "DEF"

        is_def = pos == "DEF"
        def_team = get_def_team(college_team, name) if is_def else None

        rows.append(
            {
                "name": name,
                "external_id": pid_str,
                "college_team": college_team,
                "def_team": def_team,
                "position": pos,
                "roster_status": "free_agent",
                "lineup_status": None,
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
        f"[fantrax-cfb] fetch_fantrax_players leagueId={league_id} "
        f"period={period}: {len(rows)} players (rostered + free agents)",
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
        league_row = conn.execute(
            text(
                "select id from leagues "
                "where platform = :platform "
                "  and external_league_id::text = :external_league_id "
                "  and season = :season"
            ),
            {
                "platform": FANTRAX_PLATFORM,
                "external_league_id": league_external_id,
                "season": FANTRAX_SEASON,
            },
        ).fetchone()

        if league_row is None:
            print(
                f"[fantrax-cfb] No leagues row for platform={FANTRAX_PLATFORM}; "
                "skipping history.",
                file=sys.stderr,
            )
            return

        league_id = league_row[0]

        for r in rows:
            ext_key = str(r.get("external_id", "") or "")

            player_payload = {
                "college_team": r.get("college_team"),
                "note_type": r.get("note_type"),
                "raw_row_text": r.get("raw_row_text"),
            }
            if r.get("def_team"):
                player_payload["def_team"] = r.get("def_team")

            try:
                player_row = conn.execute(
                    text(
                        """
                        insert into players
                          (platform, external_player_key, player_name, pos, sport, payload)
                        values
                          (:platform, :external_player_key, :player_name, :pos, :sport, :payload)
                        on conflict on constraint ux_players_platform_extkey do update set
                          player_name       = excluded.player_name,
                          pos               = excluded.pos,
                          payload           = players.payload || excluded.payload::jsonb
                        returning id
                        """
                    ),
                    {
                        "platform": FANTRAX_PLATFORM,
                        "external_player_key": ext_key,
                        "player_name": r["name"],
                        "pos": r["position"],
                        "sport": FANTRAX_SPORT,
                        "payload": json.dumps(player_payload),
                    },
                ).fetchone()
            except IntegrityError as e:
                print(
                    f"[fantrax-cfb] IntegrityError for player "
                    f"{r.get('name')} {r.get('college_team')} "
                    f"{r.get('position')}: {e}",
                    file=sys.stderr,
                )
                continue

            if player_row is None:
                player_row = conn.execute(
                    text(
                        """
                        select id from players
                        where platform = :platform
                          and external_player_key = :external_player_key
                        """
                    ),
                    {
                        "platform": FANTRAX_PLATFORM,
                        "external_player_key": ext_key,
                    },
                ).fetchone()

            if player_row is None:
                continue

            player_id = player_row[0]

            history_payload = {"college_team": r.get("college_team")}
            if r.get("def_team"):
                history_payload["def_team"] = r.get("def_team")

            try:
                conn.execute(
                    text(
                        """
                        insert into roster_status_history
                          (league_id, player_id, fantasy_team, roster_status,
                           lineup_status, position, fetched_at, payload)
                        values
                          (:league_id, :player_id, :fantasy_team, :roster_status,
                           :lineup_status, :position, :fetched_at, :payload)
                        """
                    ),
                    {
                        "league_id": league_id,
                        "player_id": player_id,
                        "fantasy_team": r.get("fantasy_team"),
                        "roster_status": r.get("roster_status"),
                        "lineup_status": r.get("lineup_status"),
                        "position": r.get("position"),
                        "fetched_at": fetched_at,
                        "payload": json.dumps(history_payload),
                    },
                )
            except IntegrityError as e:
                print(
                    f"[fantrax-cfb] history insert error for player_id={player_id}: {e}",
                    file=sys.stderr,
                )
                continue

    print(
        f"[fantrax-cfb] Upserted {len(rows)} rows for "
        f"league_external_id={league_external_id}, "
        f"snapshot fetched_at={fetched_at.isoformat()}",
        flush=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    league_ids = get_league_ids()
    if not league_ids:
        print("[fantrax-cfb] No FANTRAX_LEAGUE_IDS set; exiting.", file=sys.stderr)
        return

    # Build directory once — shared across all leagues
    player_directory = fetch_player_directory()

    for league_id in league_ids:
        print(f"[fantrax-cfb] Syncing league {league_id}", flush=True)
        rows = fetch_fantrax_players(league_id, player_directory)
        if rows:
            upsert_players_and_history(league_id, rows)
        else:
            print(
                f"[fantrax-cfb] No rows for league {league_id}; skipping upsert.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()