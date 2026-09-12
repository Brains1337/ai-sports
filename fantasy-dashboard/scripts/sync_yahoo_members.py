#!/usr/bin/env python3
"""
sync_yahoo_members.py — sync Yahoo CFB league managers/teams page.

Scrapes https://college.fantasysports.yahoo.com/cfb/{league_id}/teams
and upserts member profiles into leagues_members + league_members.

This keeps waiver priorities, manager names, and team affiliations in sync
throughout the season. Uses the same Playwright storage_state auth as
sync_yahoo.py.

Auth: same YAHOO_STATE_B64 / YAHOO_STATE_PATH as sync_yahoo.py.

Usage:
    YAHOO_LEAGUE_IDS=37494 YAHOO_STATE_B64=... python sync_yahoo_members.py
"""

import base64
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, DataError

DATABASE_URL = os.environ["DATABASE_URL"]
YAHOO_LEAGUE_IDS = os.getenv("YAHOO_LEAGUE_IDS", os.getenv("YAHOO_LEAGUE_ID", "37494"))
YAHOO_SEASON = int(os.getenv("YAHOO_SEASON", "2026"))
YAHOO_STATE_B64 = os.getenv("YAHOO_STATE_B64", "")
YAHOO_STATE_PATH = os.getenv("YAHOO_STATE_PATH", "")
YAHOO_PLATFORM = os.getenv("YAHOO_PLATFORM", "yahoo-cfb")

# Scraper artifact patterns — must not store these as fantasy_team names
_INVALID_TEAM_RE = re.compile(
    r"^FA$"
    r"|^Free\s+agent$"
    r"|^Free$"
    r"|^[WL]\s*\("
    r"|^[\d\s.\-]+$"
    r"|^Q[1-4]$"
    r"|^(?:Sat|Sun|Mon|Tue|Wed|Thu|Fri)$"
    r"|^(?:Final|Live|1st|2nd|3rd|4th)$"
    r"|^Owned\b"
    r"|^Owned\s*·"
    r"|^Owned\s+\.\s+"
)


def is_valid_team_name(name: str | None) -> bool:
    """Return False for scraper artifacts."""
    if not name or len(name.strip()) < 2:
        return False
    stripped = name.strip()
    if stripped[0].isdigit():
        return False
    if _INVALID_TEAM_RE.match(stripped):
        return False
    # Reject multi-token candidates containing game-status tokens
    game_status_tokens = {
        "sat", "sun", "mon", "tue", "wed", "thu", "fri",
        "final", "live", "am", "pm", "1st", "2nd", "3rd", "4th",
    }
    for tok in stripped.split():
        if tok.lower() in game_status_tokens:
            return False
    return True


def now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_state_path() -> str:
    """Resolve the Playwright storage_state path from env."""
    if YAHOO_STATE_PATH:
        return YAHOO_STATE_PATH
    if YAHOO_STATE_B64:
        state_json = base64.b64decode(YAHOO_STATE_B64)
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False, prefix="yahoo_state_"
        )
        tmp.write(state_json)
        tmp.close()
        return tmp.name
    raise RuntimeError("No Yahoo auth state configured. Set YAHOO_STATE_B64 or YAHOO_STATE_PATH.")


def parse_teams_page(content: str) -> List[Dict[str, Any]]:
    """Parse the Yahoo teams page HTML to extract manager/team info.

    The teams page has a table with columns:
    Team Name | Manager | Email | Waiver Priority | Moves | Trades | Last Activity

    Each team name cell may contain an <img> with the logo, and the
    manager cell may contain the Yahoo display name.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    teams: List[Dict[str, Any]] = []

    # Find the teams table — Yahoo uses various table IDs/classes
    # The teams page has a table where each row is a team
    table = soup.find("table")
    if not table:
        print("[yahoo-members] WARNING: no table found on teams page", file=sys.stderr)
        return teams

    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        # Cell 0: Team Name (may contain <img> logo + text)
        # Cell 1: Manager name
        # Cell 2: Email (may be --hidden--)
        # Cell 3: Waiver Priority
        # Cell 4: Moves
        # Cell 5: Trades
        # Cell 6: Last League Activity

        team_name = cells[0].get_text(strip=True) if len(cells) > 0 else ""
        manager_name = cells[1].get_text(strip=True) if len(cells) > 1 else ""

        # Skip the header row
        if team_name.lower() in ("team", "team name") or not team_name:
            continue
        if manager_name.lower() in ("manager",) or not manager_name:
            continue

        # Clean up team name — strip leading/trailing whitespace
        team_name = team_name.strip()

        # Validate team name — skip scraper artifacts
        if not is_valid_team_name(team_name):
            continue

        # Email may be hidden for privacy
        email_cell = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        email = ""
        if email_cell and email_cell not in ("--hidden--", "--", ""):
            # Extract email from text or data attribute
            email = email_cell

        waiver_priority = None
        wp_text = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        if wp_text:
            try:
                waiver_priority = int(wp_text)
            except ValueError:
                pass

        moves = None
        moves_text = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        if moves_text:
            try:
                moves = int(moves_text)
            except ValueError:
                pass

        trades = None
        trades_text = cells[5].get_text(strip=True) if len(cells) > 5 else ""
        if trades_text:
            try:
                trades = int(trades_text)
            except ValueError:
                pass

        last_activity = cells[6].get_text(strip=True) if len(cells) > 6 else ""

        # Try to extract external_member_id from the team link
        team_link = cells[0].find("a")
        external_member_key = None
        if team_link:
            href = team_link.get("href", "")
            # Yahoo team URLs look like: /cfb/37494/team123456
            match = re.search(r"/cfb/(\d+)/team(\d+)", href)
            if match:
                external_member_key = f"{match.group(1)}:{match.group(2)}"

        teams.append({
            "team_name": team_name,
            "manager_name": manager_name,
            "manager_email": email if email else None,
            "waiver_priority": waiver_priority,
            "moves": moves,
            "trades": trades,
            "last_activity": last_activity,
            "external_member_key": external_member_key,
        })

    print(f"[yahoo-members] parsed {len(teams)} teams from page", file=sys.stderr)
    return teams


def upsert_members(conn, league_id: int, league_key: str, teams: List[Dict[str, Any]]) -> int:
    """Upsert member profiles and league_member rows."""
    fetched_at = now()
    upserted_members = 0
    upserted_league_members = 0

    for team in teams:
        team_name = team["team_name"]
        manager_name = team["manager_name"]

        # Validate team name again (defensive)
        if not is_valid_team_name(team_name):
            print(f"[yahoo-members] WARN: skipping invalid team name: {team_name!r}", file=sys.stderr)
            continue

        # External member key — use league_key:team_name as a fallback
        ext_key = team.get("external_member_key") or f"{league_key}:{team_name}"

        # Upsert into leagues_members
        conn.execute(
            text("""
                INSERT INTO leagues_members
                    (platform, external_member_key, manager_name, manager_email,
                     payload, updated_at)
                VALUES
                    (:platform, :ext_key, :manager_name, :manager_email,
                     cast(:payload as jsonb), now())
                ON CONFLICT (platform, external_member_key) DO UPDATE SET
                    manager_name  = excluded.manager_name,
                    manager_email = excluded.manager_email,
                    payload       = leagues_members.payload || excluded.payload::jsonb,
                    updated_at    = now()
            """),
            {
                "platform": YAHOO_PLATFORM,
                "ext_key": ext_key,
                "manager_name": manager_name,
                "manager_email": team.get("manager_email"),
                "payload": json.dumps({
                    "source": "sync_yahoo_members",
                    "waiver_priority": team.get("waiver_priority"),
                    "moves": team.get("moves"),
                    "trades": team.get("trades"),
                    "last_activity": team.get("last_activity"),
                    "league_key": league_key,
                    "fetched_at": fetched_at.isoformat(),
                }),
            },
        )

        # Get the member_id back
        member_row = conn.execute(
            text("""
                SELECT id FROM leagues_members
                WHERE platform = :platform AND external_member_key = :ext_key
            """),
            {"platform": YAHOO_PLATFORM, "ext_key": ext_key},
        ).fetchone()

        if member_row is None:
            print(f"[yahoo-members] ERROR: could not retrieve member_id for {team_name}", file=sys.stderr)
            continue

        member_id = member_row[0]
        upserted_members += 1

        # Upsert into league_members (links member to this league + team)
        conn.execute(
            text("""
                INSERT INTO league_members
                    (league_id, member_id, fantasy_team, waiver_priority,
                     team_slot, payload, updated_at)
                VALUES
                    (:league_id, :member_id, :fantasy_team, :waiver_priority,
                     :team_slot, cast(:payload as jsonb), now())
                ON CONFLICT (league_id, fantasy_team) DO UPDATE SET
                    member_id       = excluded.member_id,
                    waiver_priority = excluded.waiver_priority,
                    team_slot       = excluded.team_slot,
                    payload         = league_members.payload || excluded.payload::jsonb,
                    updated_at      = now()
            """),
            {
                "league_id": league_id,
                "member_id": member_id,
                "fantasy_team": team_name,
                "waiver_priority": team.get("waiver_priority"),
                "team_slot": team.get("waiver_priority"),  # Yahoo team order = waiver priority
                "payload": json.dumps({
                    "source": "sync_yahoo_members",
                    "manager_email": team.get("manager_email") or "",
                    "moves": team.get("moves"),
                    "trades": team.get("trades"),
                    "last_activity": team.get("last_activity"),
                }),
            },
        )
        upserted_league_members += 1

    print(
        f"[yahoo-members] league_key={league_key}: "
        f"upserted {upserted_members} members, {upserted_league_members} league_members",
        flush=True,
    )
    return upserted_members


def get_league_keys() -> List[str]:
    """Get the list of Yahoo league IDs to sync."""
    keys = [k.strip() for k in YAHOO_LEAGUE_IDS.split(",") if k.strip()]
    return keys


def sync_roster_assignments(conn, league_id: int, league_key: str) -> int:
    """Populate roster_assignments from the latest roster_status_history snapshot.

    For each player in roster_status_history (latest snapshot per player
    for this league), look up:
      - member_id from league_members (by fantasy_team name)
      - cfbd_athlete_id from players.payload
      - player_id from players.id

    Then upsert into roster_assignments with valid_to handling:
    - If the player's current assignment matches (same athlete_id/member_id,
      valid_to IS NULL), skip (no change).
    - If changed, set valid_to on the old row and insert a new row.
    """
    fetched_at = now()

    # Get the latest roster_status_history snapshot for this league
    # and join to league_members and players to get member_id and cfbd_athlete_id
    rows = conn.execute(
        text("""
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
                lms.member_id,
                lms.fantasy_team,
                lms.waiver_priority,
                rsh.player_id,
                (p.payload->>'cfbd_athlete_id')::text as athlete_id,
                rsh.roster_status,
                rsh.lineup_status,
                rsh.slot_name
            from latest rsh
            join leagues l on l.id = rsh.league_id
            join league_members lms on lms.league_id = l.id
                and lms.fantasy_team = rsh.fantasy_team
            join players p on p.id = rsh.player_id
            where lms.member_id is not null
        """),
        {"league_id": league_id},
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

        # Check if this athlete is already assigned to this member (active)
        existing = conn.execute(
            text("""
                select id from roster_assignments
                where league_id = :league_id
                  and member_id = :member_id
                  and (athlete_id = :athlete_id or player_id = :player_id)
                  and valid_to is null
            """),
            {
                "league_id": league_id_val,
                "member_id": member_id,
                "athlete_id": athlete_id,
                "player_id": player_id,
            },
        ).fetchone()

        if existing:
            # Already assigned — check if roster_status changed
            conn.execute(
                text("""
                    update roster_assignments
                    set roster_status = :roster_status,
                        lineup_status = :lineup_status,
                        slot_name = :slot_name,
                        fetched_at = now()
                    where id = :id
                """),
                {
                    "id": existing[0],
                    "roster_status": row["roster_status"],
                    "lineup_status": row["lineup_status"],
                    "slot_name": row["slot_name"],
                },
            )
            continue

        # Check if this athlete was previously assigned to someone else (needs valid_to)
        conn.execute(
            text("""
                update roster_assignments
                set valid_to = now()
                where league_id = :league_id
                  and (athlete_id = :athlete_id or player_id = :player_id)
                  and valid_to is null
            """),
            {
                "league_id": league_id_val,
                "athlete_id": athlete_id,
                "player_id": player_id,
            },
        )

        # Insert new assignment
        conn.execute(
            text("""
                insert into roster_assignments
                    (league_id, member_id, athlete_id, player_id,
                     valid_from, valid_to, roster_status, lineup_status,
                     slot_name, source_name, season, sport, fetched_at, payload)
                values
                    (:league_id, :member_id, :athlete_id, :player_id,
                     now(), null, :roster_status, :lineup_status,
                     :slot_name, :source_name, :season, :sport, :fetched_at,
                     cast(:payload as jsonb))
            """),
            {
                "league_id": league_id_val,
                "member_id": member_id,
                "athlete_id": athlete_id,
                "player_id": player_id,
                "roster_status": row["roster_status"],
                "lineup_status": row["lineup_status"],
                "slot_name": row["slot_name"],
                "source_name": "sync_yahoo_members",
                "season": season,
                "sport": sport,
                "fetched_at": fetched_at,
                "payload": json.dumps({"fantasy_team": fantasy_team}),
            },
        )
        inserted += 1

    print(
        f"[yahoo-members] league_key={league_key}: "
        f"upserted {inserted} roster_assignments",
        flush=True,
    )
    return inserted


def main() -> None:
    league_keys = get_league_keys()
    if not league_keys:
        print("No YAHOO_LEAGUE_IDS configured; nothing to sync.", file=sys.stderr)
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright required: pip install playwright "
            "&& python -m playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    state_path = resolve_state_path()
    cleanup_temp = YAHOO_STATE_B64 != ""

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=state_path)
            page = context.new_page()
            page.set_default_timeout(60000)

            for league_key in league_keys:
                print(f"[yahoo-members] Syncing teams for league {league_key}", flush=True)

                # Look up the internal league_id
                with engine.begin() as conn:
                    league_row = conn.execute(
                        text("""
                            SELECT id FROM leagues
                            WHERE external_league_key = :league_key
                              AND platform = :platform
                              AND season = :season
                        """),
                        {
                            "league_key": league_key,
                            "platform": YAHOO_PLATFORM,
                            "season": YAHOO_SEASON,
                        },
                    ).fetchone()

                    if league_row is None:
                        print(
                            f"[yahoo-members] No leagues row for "
                            f"external_league_key={league_key} "
                            f"platform={YAHOO_PLATFORM} season={YAHOO_SEASON}",
                            file=sys.stderr,
                        )
                        continue

                    league_id = league_row[0]

                # Scrape the teams page
                url = f"https://college.fantasysports.yahoo.com/cfb/{league_key}/teams"
                print(f"[yahoo-members] Fetching {url}", flush=True)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)  # Let JS render

                content = page.content()
                teams = parse_teams_page(content)

                if not teams:
                    print(
                        f"[yahoo-members] No teams found for league {league_key}. "
                        "The page may require login or have a different structure.",
                        file=sys.stderr,
                    )
                    continue

                with engine.begin() as conn:
                    upsert_members(conn, league_id, league_key, teams)

            context.close()
            browser.close()
    finally:
        if cleanup_temp and os.path.exists(state_path):
            os.remove(state_path)

    # Phase 2: Sync roster_assignments from roster_status_history
    # This runs after members are synced, so we have member_id <-> fantasy_team
    # mappings to link against. Also links CFBD athlete IDs via players.payload.
    for league_key in league_keys:
        with engine.begin() as conn:
            league_row = conn.execute(
                text("""
                    SELECT id FROM leagues
                    WHERE external_league_key = :league_key
                      AND platform = :platform
                      AND season = :season
                """),
                {
                    "league_key": league_key,
                    "platform": YAHOO_PLATFORM,
                    "season": YAHOO_SEASON,
                },
            ).fetchone()

            if league_row is None:
                print(
                    f"[yahoo-members] No leagues row for league_key={league_key}",
                    file=sys.stderr,
                )
                continue

            sync_roster_assignments(conn, league_row[0], league_key)


if __name__ == "__main__":
    main()
