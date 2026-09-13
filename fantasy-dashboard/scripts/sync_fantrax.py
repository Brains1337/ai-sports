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
                "team": p.get("team") or p.get("teamName") or p.get("teamShortName"),  # college team — Fantrax returns teamName/teamShortName
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
                "team": p.get("team") or p.get("teamName") or p.get("teamShortName"),
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
                directory[pid_str]["team"] = (
                    p.get("team")
                    or p.get("teamName")
                    or p.get("teamShortName")
                    or p.get("proTeam")
                )
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
# Member sync (leagues_members — consolidated)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_fantrax_members(league_id: str) -> List[Dict[str, Any]]:
    """
    Fetch team/owner info from getLeagueInfo's teamInfo.

    Fantrax's teamInfo is a list of team dicts. Each contains team identification
    and owner/manager details. Field names vary across Fantrax API versions, so
    we defensively try multiple possible keys.

    Returns list of dicts with keys:
      team_id, fantasy_team, team_abbrev, external_member_key,
      manager_name, manager_email, waiver_priority, payload
    """
    headers = make_headers()
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
            f"[fantrax-cfb] getLeagueInfo failed for members leagueId={league_id}: {e}",
            file=sys.stderr,
        )
        return []

    raw_team_info = info.get("teamInfo") or []
    # Fantrax sometimes returns teamInfo as a dict keyed by teamId
    if isinstance(raw_team_info, dict):
        print(
            f"[fantrax-cfb] teamInfo was dict with {len(raw_team_info)} keys; "
            f"converting to list",
            file=sys.stderr,
        )
        raw_team_info = list(raw_team_info.values())
    if not isinstance(raw_team_info, list):
        print(
            f"[fantrax-cfb] teamInfo not a list (type={type(raw_team_info)})",
            file=sys.stderr,
        )
        return []

    if raw_team_info:
        try:
            print(
                "[fantrax-cfb] sample teamInfo entry:",
                json.dumps(raw_team_info[0], indent=2)[:800],
                file=sys.stderr,
            )
        except Exception:
            pass

    teams: List[Dict[str, Any]] = []
    for idx, team in enumerate(raw_team_info):
        if not isinstance(team, dict):
            continue

        team_id = (
            team.get("teamId")
            or team.get("id")
            or team.get("teamID")
            or str(idx + 1)
        )
        team_id = str(team_id)

        fantasy_team = (
            team.get("teamName")
            or team.get("name")
            or team.get("displayName")
            or f"Team {team_id}"
        )

        team_abbrev = (
            team.get("abbreviation")
            or team.get("abbrev")
            or team.get("teamAbbrev")
            or team.get("shortName")
            or ""
        )

        # Owner/manager fields — Fantrax uses multiple naming conventions.
        # Try the most likely keys for username/real name/email.
        manager_name = (
            team.get("ownerName")
            or team.get("managerName")
            or team.get("userName")
            or team.get("username")
            or team.get("userId")
            or team.get("ownerUserId")
            or team.get("managerId")
        )

        manager_email = (
            team.get("ownerEmail")
            or team.get("managerEmail")
            or team.get("email")
            or team.get("userEmail")
        )

        waiver_priority = None
        wp_val = (
            team.get("waiverPriority")
            or team.get("waiver_priority")
            or team.get("waiverOrder")
            or team.get("draftOrder")
        )
        if wp_val is not None:
            try:
                waiver_priority = int(wp_val)
            except (ValueError, TypeError):
                pass

        external_member_key = f"{league_id}:{team_id}"

        teams.append(
            {
                "team_id": team_id,
                "fantasy_team": fantasy_team,
                "team_abbrev": team_abbrev,
                "external_member_key": external_member_key,
                "manager_name": manager_name if manager_name else None,
                "manager_email": manager_email if manager_email else None,
                "waiver_priority": waiver_priority,
                "payload": json.dumps(team, separators=(",", ":")),
            }
        )

    print(
        f"[fantrax-cfb] fetched {len(teams)} teams for members leagueId={league_id}",
        file=sys.stderr,
    )
    return teams


def upsert_fantrax_members(
    league_id: str, league_db_id: int, teams: List[Dict[str, Any]]
) -> int:
    """
    Upsert into leagues_members (consolidated — includes league context).

    Mirrors sync_yahoo_members.upsert_members — now writes a single row per
    (platform, external_member_key, league_id) into leagues_members,
    eliminating the league_members join table (migration 024).
    """
    if not teams:
        return 0

    upserted = 0
    with engine.begin() as conn:
        for team in teams:
            ext_key = team["external_member_key"]

            # Upsert into leagues_members (consolidated: profile + league context)
            conn.execute(
                text(
                    """
                    INSERT INTO leagues_members
                        (platform, external_member_key, manager_name, manager_email,
                         payload, league_id, fantasy_team, waiver_priority,
                         team_slot, source_name, updated_at)
                    VALUES
                        (:platform, :ext_key, :manager_name, :manager_email,
                         cast(:payload as jsonb), :league_id, :fantasy_team,
                         :waiver_priority, :team_slot, :source_name, now())
                    ON CONFLICT (platform, external_member_key, league_id) DO UPDATE SET
                        manager_name    = excluded.manager_name,
                        manager_email   = excluded.manager_email,
                        payload         = leagues_members.payload || excluded.payload::jsonb,
                        fantasy_team    = excluded.fantasy_team,
                        waiver_priority = excluded.waiver_priority,
                        team_slot       = excluded.team_slot,
                        source_name     = excluded.source_name,
                        updated_at      = now()
                    """
                ),
                {
                    "platform": FANTRAX_PLATFORM,
                    "ext_key": ext_key,
                    "manager_name": team.get("manager_name"),
                    "manager_email": team.get("manager_email"),
                    "payload": json.dumps(
                        {
                            "source": "sync_fantrax",
                            "team_id": team.get("team_id"),
                            "team_abbrev": team.get("team_abbrev"),
                            "waiver_priority": team.get("waiver_priority"),
                            "league_key": league_id,
                            "fetched_at": now().isoformat(),
                        }
                    ),
                    "league_id": league_db_id,
                    "fantasy_team": team["fantasy_team"],
                    "waiver_priority": team.get("waiver_priority"),
                    "team_slot": team.get("waiver_priority"),  # Fantrax order = waiver priority
                    "source_name": "fantrax-cfb",
                },
            )
            upserted += 1

    print(
        f"[fantrax-cfb] leagueId={league_id}: "
        f"upserted {upserted} members (consolidated into leagues_members)",
        flush=True,
    )
    return upserted

def sync_fantrax_roster_assignments(league_db_id: int, league_external_key: str) -> int:
    """
    Populate roster_assignments from roster_status_history for a Fantrax league.

    Mirrors sync_yahoo_members.sync_roster_assignments — links rostered players
    to leagues_members via fantasy_team, and attaches CFBD athlete_id from
    players.payload. Also syncs roster players to leagues_members if any are
    missing (handles teams whose owner info wasn't in teamInfo).
    """
    fetched_at = now()

    with engine.begin() as conn:
        # First, ensure all fantasy_teams in this league's roster_status_history
        # have a leagues_members entry. If a team is missing, create a fallback
        # member so roster_assignments foreign keys resolve.
        missing_teams = conn.execute(
            text(
                """
                select distinct rsh.fantasy_team
                from roster_status_history rsh
                where rsh.league_id = :league_id
                  and rsh.fantasy_team is not null
                  and rsh.fantasy_team != ''
                  and not exists (
                    select 1 from leagues_members lms
                    where lms.league_id = rsh.league_id
                      and lms.fantasy_team = rsh.fantasy_team
                  )
                """
            ),
            {"league_id": league_db_id},
        ).mappings().all()

        for mt in missing_teams:
            fantasy_team = mt["fantasy_team"]
            ext_key = f"{league_external_key}:{fantasy_team}"

            # Insert a fallback into leagues_members (consolidated)
            conn.execute(
                text(
                    """
                    INSERT INTO leagues_members
                        (platform, external_member_key, manager_name, manager_email,
                         payload, league_id, fantasy_team, waiver_priority,
                         team_slot, source_name, updated_at)
                    VALUES
                        (:platform, :ext_key, :manager_name, :manager_email,
                         cast(:payload as jsonb), :league_id, :fantasy_team,
                         null, null, :source_name, now())
                    ON CONFLICT (platform, external_member_key, league_id) DO NOTHING
                    """
                ),
                {
                    "platform": FANTRAX_PLATFORM,
                    "ext_key": ext_key,
                    "manager_name": fantasy_team,
                    "manager_email": None,
                    "payload": json.dumps(
                        {"source": "sync_fantrax_fallback", "fetched_at": fetched_at.isoformat()}
                    ),
                    "league_id": league_db_id,
                    "fantasy_team": fantasy_team,
                    "source_name": "fantrax-cfb",
                },
            )

        # Now upsert roster_assignments from latest roster_status_history snapshot
        rows = conn.execute(
            text(
                """
                with latest as (
                    select distinct on (player_id)
                        player_id, league_id, fantasy_team, roster_status,
                        lineup_status, slot_name, fetched_at
                    from roster_status_history
                    where league_id = :league_id
                      and fantasy_team is not null
                      and fantasy_team != ''
                    order by player_id, fetched_at desc
                )
                select
                    l.id as league_id,
                    l.season,
                    l.sport,
                    lms.id as member_id,
                    lms.fantasy_team,
                    rsh.player_id,
                    (p.payload->>'cfbd_athlete_id')::text as athlete_id,
                    rsh.roster_status,
                    rsh.lineup_status,
                    rsh.slot_name
                from latest rsh
                join leagues l on l.id = rsh.league_id
                join leagues_members lms on lms.league_id = l.id
                    and lms.fantasy_team = rsh.fantasy_team
                join players p on p.id = rsh.player_id
                where lms.id is not null
                """
            ),
            {"league_id": league_db_id},
        ).mappings().all()

        inserted = 0
        for row in rows:
            athlete_id = row["athlete_id"]
            player_id = row["player_id"]
            member_id = row["member_id"]
            league_id_val = row["league_id"]
            season = row["season"]
            sport = row["sport"]
            fantasy_team = row["fantasy_team"]

            # Check if already assigned (active, no valid_to)
            existing = conn.execute(
                text(
                    """
                    select id from roster_assignments
                    where league_id = :league_id
                      and member_id = :member_id
                      and (athlete_id = :athlete_id or player_id = :player_id)
                      and valid_to is null
                    """
                ),
                {
                    "league_id": league_id_val,
                    "member_id": member_id,
                    "athlete_id": athlete_id,
                    "player_id": player_id,
                },
            ).fetchone()

            if existing:
                updates = {
                    "id": existing[0],
                    "roster_status": row["roster_status"],
                    "lineup_status": row["lineup_status"],
                    "slot_name": row["slot_name"],
                }
                if athlete_id is not None:
                    conn.execute(
                        text(
                            """
                            update roster_assignments
                            set roster_status = :roster_status,
                                lineup_status = :lineup_status,
                                slot_name = :slot_name,
                                athlete_id = :athlete_id,
                                fetched_at = now()
                            where id = :id
                            """
                        ),
                        {**updates, "athlete_id": athlete_id},
                    )
                else:
                    conn.execute(
                        text(
                            """
                            update roster_assignments
                            set roster_status = :roster_status,
                                lineup_status = :lineup_status,
                                slot_name = :slot_name,
                                fetched_at = now()
                            where id = :id
                            """
                        ),
                        updates,
                    )
                continue

            # Close out any previous assignment for this athlete/player
            conn.execute(
                text(
                    """
                    update roster_assignments
                    set valid_to = now()
                    where league_id = :league_id
                      and (athlete_id = :athlete_id or player_id = :player_id)
                      and valid_to is null
                    """
                ),
                {
                    "league_id": league_id_val,
                    "athlete_id": athlete_id,
                    "player_id": player_id,
                },
            )

            conn.execute(
                text(
                    """
                    insert into roster_assignments
                        (league_id, member_id, athlete_id, player_id,
                         valid_from, valid_to, roster_status, lineup_status,
                         slot_name, source_name, season, sport, fetched_at, payload)
                    values
                        (:league_id, :member_id, :athlete_id, :player_id,
                         now(), null, :roster_status, :lineup_status,
                         :slot_name, :source_name, :season, :sport, :fetched_at,
                         cast(:payload as jsonb))
                    """
                ),
                {
                    "league_id": league_id_val,
                    "member_id": member_id,
                    "athlete_id": athlete_id,
                    "player_id": player_id,
                    "roster_status": row["roster_status"],
                    "lineup_status": row["lineup_status"],
                    "slot_name": row["slot_name"],
                    "source_name": "sync_fantrax",
                    "season": season,
                    "sport": sport,
                    "fetched_at": fetched_at,
                    "payload": json.dumps({"fantasy_team": fantasy_team}),
                },
            )
            inserted += 1

    print(
        f"[fantrax-cfb] leagueId={league_external_key}: "
        f"upserted {inserted} roster_assignments",
        flush=True,
    )
    return inserted


# ──────────────────────────────────────────────────────────────────────────────
# DB upsert
# ──────────────────────────────────────────────────────────────────────────────


def upsert_players_and_history(
    league_external_key: str, rows: List[Dict[str, Any]]
) -> None:
    fetched_at = now()
    with engine.begin() as conn:
        league_row = conn.execute(
            text(
                "select id from leagues "
                "where platform = :platform "
                "  and external_league_key = :external_league_key "
                "  and season = :season"
            ),
            {
                "platform": FANTRAX_PLATFORM,
                "external_league_key": league_external_key,
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
                        on conflict (platform, external_player_key) do update set
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
                conn.rollback()
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
                conn.rollback()
                continue

    print(
        f"[fantrax-cfb] Upserted {len(rows)} rows for "
        f"league_external_key={league_external_key}, "
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

        # Sync members (leagues_members) from teamInfo
        # and then populate roster_assignments via the cross-reference.
        with engine.begin() as conn:
            league_row = conn.execute(
                text(
                    "select id from leagues "
                    "where platform = :platform "
                    "  and external_league_key = :external_league_key "
                    "  and season = :season"
                ),
                {
                    "platform": FANTRAX_PLATFORM,
                    "external_league_key": league_id,
                    "season": FANTRAX_SEASON,
                },
            ).fetchone()

            if league_row is None:
                print(
                    f"[fantrax-cfb] No leagues row for platform={FANTRAX_PLATFORM} "
                    f"external_league_key={league_id}; skipping members/roster_assignments.",
                    file=sys.stderr,
                )
                continue

            league_db_id = league_row[0]
            member_teams = fetch_fantrax_members(league_id)
            if member_teams:
                upsert_fantrax_members(league_id, league_db_id, member_teams)
            else:
                print(
                    f"[fantrax-cfb] No member teams returned for league {league_id}",
                    file=sys.stderr,
                )
            sync_fantrax_roster_assignments(league_db_id, league_id)


if __name__ == "__main__":
    main()