#!/usr/bin/env python3
"""
sync_yahoo.py — Season-long Yahoo college fantasy roster sync.

Scrapes status=ALL (every player: rostered + free agent + waivers) for each
position in each configured Yahoo CFB league, upserts players into the shared
`players` table, and inserts snapshot rows per player into `roster_status_history`
for season-long tracking of adds/drops/trades.

Auth: the Yahoo Playwright storage_state is read from the YAHOO_STATE_B64
env var (base64-encoded JSON), or optionally from YAHOO_STATE_PATH (mounted
file). Use encode_yahoo_state.py to produce YAHOO_STATE_B64 once, then refresh
it whenever the Yahoo session expires.

Requires: playwright, sqlalchemy, psycopg[binary]
One-time setup in the container:
    python -m playwright install --with-deps chromium
"""

import base64
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode

from sqlalchemy import create_engine, text
from psycopg import ProgrammingError  # guard against dict/json issues

from teams_normalizer import get_def_team  # NEW

DATABASE_URL = os.environ["DATABASE_URL"]

# Support multiple Yahoo CFB leagues; comma-separated IDs.
# Example: YAHOO_LEAGUE_IDS=37494,12345
YAHOO_LEAGUE_IDS = os.getenv("YAHOO_LEAGUE_IDS", os.getenv("YAHOO_LEAGUE_ID", "37494"))
YAHOO_SEASON = int(os.getenv("YAHOO_SEASON", "2026"))
YAHOO_STATE_B64 = os.getenv("YAHOO_STATE_B64", "")
YAHOO_STATE_PATH = os.getenv("YAHOO_STATE_PATH", "")  # optional fallback: mounted file
MY_TEAM_NAME = os.getenv("MY_TEAM_NAME", "").strip()  # explicit team name override

# Platform label must match leagues.platform and what the dashboard expects.
# Your DB row uses 'yahoo-cfb' for Yahoo EDIT League.
YAHOO_PLATFORM = os.getenv("YAHOO_PLATFORM", "yahoo-cfb")

PAGE_SIZE = 25
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]

# Pattern to match "MIA - QB" style college team and position
TEAM_POS_RE = re.compile(r"\b([A-Za-z]{2,6})\s*-\s*(QB|RB|WR|TE|K|DEF)\b")
NOTE_PHRASES = ["No new player Notes", "New Player Note", "Player Note"]

# Unicode apostrophe/quote characters Yahoo renders in team names.
# Normalize all of these to a plain ASCII apostrophe before any DB write.
_APOSTROPHE_CHARS = (
    "\u2019",  # RIGHT SINGLE QUOTATION MARK '
    "\u2018",  # LEFT SINGLE QUOTATION MARK '
    "\u02bc",  # MODIFIER LETTER APOSTROPHE '
    "\u0060",  # GRAVE ACCENT `
    "\u00b4",  # ACUTE ACCENT ´
)

# Scraper-artifact patterns that must never be stored as a fantasy_team name.
_INVALID_TEAM_RE = re.compile(
    r"^FA$"           # free agent label
    r"|^[WL]\s*\("    # "W (Sep 9)" / "L (Sep 9)" game results
    r"|^[\d\s.\-]+$"  # all-numeric/whitespace garbage
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_apostrophes(s: str | None) -> str | None:
    """Replace curly/smart apostrophes with plain ASCII apostrophe."""
    if not s:
        return s
    for ch in _APOSTROPHE_CHARS:
        s = s.replace(ch, "'")
    return s


def is_valid_team_name(name: str | None) -> bool:
    """Return False for scraper artifacts. Only real team names accepted."""
    if not name or len(name.strip()) < 2:
        return False
    if name[0].isdigit():
        return False
    return not bool(_INVALID_TEAM_RE.match(name.strip()))


def get_league_ids() -> List[str]:
    raw = YAHOO_LEAGUE_IDS
    return [lid.strip() for lid in raw.split(",") if lid.strip()]


def resolve_state_path() -> str:
    """Decode YAHOO_STATE_B64 into a temp file for Playwright."""
    if YAHOO_STATE_B64:
        try:
            raw = base64.b64decode(YAHOO_STATE_B64)
            json.loads(raw)
        except Exception as e:
            print(f"YAHOO_STATE_B64 decode failed: {e}", file=sys.stderr)
            sys.exit(1)

        fd, path = tempfile.mkstemp(prefix="yahoo_state_", suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return path

    if YAHOO_STATE_PATH and os.path.exists(YAHOO_STATE_PATH):
        return YAHOO_STATE_PATH

    print("No Yahoo auth found. Set YAHOO_STATE_B64 or YAHOO_STATE_PATH.", file=sys.stderr)
    sys.exit(1)


def build_url(league_id: str, pos: str, start: int) -> str:
    params = {
        "status": "ALL",  # ALL players: rostered + free agent + waivers
        "eteam": "ALL",
        "fteam": "NONE",
        "pos": pos,
        "cut_type": "9",
        "stat1": f"S_S_{YAHOO_SEASON}",
        "myteam": "0",
        "sort": "PTS",
        "sdir": "1",
        "count": str(start),
    }
    return (
        f"https://college.fantasysports.yahoo.com/cfb/{league_id}/players?"
        f"{urlencode(params)}"
    )


def extract_text(node) -> str:
    try:
        return " ".join(node.inner_text().split())
    except Exception:
        return ""


def extract_fantasy_team_from_row(row_div, row_text: str) -> Tuple[str, str | None]:
    """Extract fantasy team and roster status from a Yahoo player row div.
    
    The new Yahoo CFB HTML structure doesn't show fantasy team affiliations 
    in the player rows directly. We need to infer ownership from:
    1. MY_TEAM_NAME env var (authoritative for our team)
    2. The "free agent" / "waiver" labels in the row
    3. Player notes which may indicate ownership changes
    
    IMPORTANT: Yahoo CFB doesn't show fantasy team affiliations in the 
    player listing - all players shown are either rostered or on waivers.
    We assume all players are "owned" unless explicitly labeled "free agent"
    or "waiver".
    
    Returns (roster_status, fantasy_team_name)
    """
    lowered = row_text.lower()
    
    # Check for explicit waiver status
    if "waiver" in lowered:
        return "waivers", None
    
    # Check for explicit free agent status - rare on this page
    if "free agent" in lowered:
        return "free_agent", None
    
    # All other players are rostered by some team
    # Check if MY_TEAM_NAME owns this player
    if MY_TEAM_NAME:
        normalized_team = normalize_apostrophes(MY_TEAM_NAME)
        if normalized_team and (
            normalized_team.lower() in lowered or 
            (MY_TEAM_NAME and MY_TEAM_NAME.lower() in lowered)
        ):
            return "owned", normalized_team
    
    # Default: player is owned by some team (not free agent)
    # My_TEAM_NAME will be applied when needed by the waiver planner
    return "owned", None


def parse_player_rows(
    page, wanted_pos: str, my_team_name: str | None = None
) -> Tuple[List[Dict[str, Any]], int]:
    """Parse player rows from the Yahoo CFB page.
    
    Updated to handle new HTML structure where rows are <div> elements,
    not <table><tr> elements.
    """
    rows: List[Dict[str, Any]] = []
    
    # Try new HTML structure first (div-based)
    # Yahoo CFB uses a responsive table-like div structure
    trs = page.locator("div.yssf-table-row")  # New structure
    if trs.count() == 0:
        # Fall back to old structure
        trs = page.locator("table tr")
    
    total = trs.count()
    if total == 0:
        # Try another selector pattern
        trs = page.locator("div.D-f.Jc-sb.Ai-c")  # Main row container
        total = trs.count()
    
    for i in range(total):
        try:
            tr = trs.nth(i)
            
            # Get player name link
            name_link = tr.locator("a.name").first
            if name_link.count() == 0:
                continue
            name = extract_text(name_link)
            if not name or len(name) < 2:
                continue
            
            # Get the full row text for analysis
            row_text = extract_text(tr)
            
            # Try to extract college team and position
            m = TEAM_POS_RE.search(row_text)
            if not m:
                continue
            college_team, pos = m.group(1), m.group(2)
            if pos != wanted_pos:
                continue

            # Extract fantasy team affiliation
            roster_status, fantasy_team = extract_fantasy_team_from_row(tr, row_text)

            # If MY_TEAM_NAME is set but wasn't found in row text, 
            # this row is owned by another team
            if fantasy_team is None and my_team_name:
                # Check if we need to look more carefully at the row
                # For now, leave as free_agent and derive later
                pass

            note_type = ""
            for phrase in NOTE_PHRASES:
                if phrase in row_text:
                    note_type = phrase
                    break

            rows.append(
                {
                    "name": name,
                    "college_team": college_team,
                    "position": pos,
                    "roster_status": roster_status,
                    "fantasy_team": fantasy_team,
                    "note_type": note_type,
                    "raw_row_text": row_text,
                }
            )
        except Exception:
            continue
    
    return rows, total


def scrape_all_positions(
    page, league_id: str, max_pages: int = 80, pause: float = 1.0
) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    for pos in POSITIONS:
        seen: set[Tuple[str, str]] = set()
        start = 0
        empty_streak = 0
        for page_no in range(1, max_pages + 1):
            url = build_url(league_id, pos, start)
            print(f"[{league_id} {pos}] page {page_no} (count={start})", flush=True)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(int(pause * 1000))
            
            try:
                page.locator("a.name").first.wait_for(timeout=10000)
            except Exception:
                pass

            rows, _ = parse_player_rows(page, pos, MY_TEAM_NAME)
            added = 0
            for r in rows:
                key = (r["name"], r["college_team"])
                if key in seen:
                    continue
                seen.add(key)
                all_rows.append(r)
                added += 1

            print(
                f"[{league_id} {pos}] matched={len(rows)} new={added}",
                flush=True,
            )

            if len(rows) == 0:
                empty_streak += 1
            else:
                empty_streak = 0

            if (
                empty_streak >= 2
                or (added == 0 and page_no > 1)
                or (len(rows) < PAGE_SIZE and page_no > 1)
            ):
                break

            start += PAGE_SIZE
    return all_rows


def derive_my_team_name(rows: List[Dict[str, Any]]) -> str | None:
    """Infer our own fantasy team name from env var or rows.
    
    Yahoo CFB player listings don't show fantasy team affiliations in the row data.
    When MY_TEAM_NAME is set, we use it for all players since we can't determine
    which team owns which player from the listing page.
    """
    # If MY_TEAM_NAME is explicitly set, use it for all players
    if MY_TEAM_NAME:
        return normalize_apostrophes(MY_TEAM_NAME) or None
    
    # Fallback: try to infer from scraped rows
    counts = Counter(
        r["fantasy_team"]
        for r in rows
        if r["roster_status"] == "owned" and r["fantasy_team"]
    )
    if counts:
        return counts.most_common(1)[0][0]
    return None


def upsert_players_and_history(
    league_external_id: str, rows: List[Dict[str, Any]]
) -> None:
    fetched_at = now()
    my_team_name = derive_my_team_name(rows)

    with engine.begin() as conn:
        league_row = conn.execute(
            text(
                "select id from leagues "
                "where external_league_id = :lid and platform = :platform"
            ),
            {"lid": int(league_external_id), "platform": YAHOO_PLATFORM},
        ).fetchone()

        if league_row is None:
            print(
                f"[yahoo-cfb] No leagues row found for external_league_id={league_external_id} "
                f"platform={YAHOO_PLATFORM}; not writing history.",
                file=sys.stderr,
            )
            return

        league_id = league_row[0]

        # Update leagues.payload with my_team_name for waiver planner
        for r in rows:
            is_def = r["position"] == "DEF"
            def_team = get_def_team(r["college_team"], r["name"]) if is_def else None

            player_payload = {
                "college_team": r["college_team"],
                "note_type": r["note_type"],
                "raw_row_text": r["raw_row_text"],
            }
            if def_team:
                player_payload["def_team"] = def_team

            try:
                # Upsert player
                player_row = conn.execute(
                    text("""
                        insert into players
                            (platform, external_player_id, player_name, pos, sport, payload)
                        values
                            (:platform, null, :name, :pos, 'NCAAF', :payload)
                        on conflict (platform, player_name, pos)
                            where external_player_id is null
                        do update set
                            payload    = players.payload || excluded.payload::jsonb,
                            updated_at = now()
                        returning id
                    """),
                    {
                        "platform": YAHOO_PLATFORM,
                        "name": r["name"],
                        "pos": r["position"],
                        "payload": json.dumps(player_payload),
                    },
                ).fetchone()
            except ProgrammingError as e:
                print(
                    f"[yahoo-cfb] ProgrammingError for player {r['name']} "
                    f"{r['college_team']} {r['position']}: {e}",
                    file=sys.stderr,
                )
                continue

            if player_row is None:
                player_row = conn.execute(
                    text("""
                        select id from players
                        where platform = :platform
                          and player_name = :name
                          and pos = :pos
                          and external_player_id is null
                        """),
                    {
                        "platform": YAHOO_PLATFORM,
                        "name": r["name"],
                        "pos": r["position"],
                    },
                ).fetchone()

            if player_row is None:
                continue

            player_id = player_row[0]

            history_payload = {"college_team": r["college_team"]}
            if def_team:
                history_payload["def_team"] = def_team

            # Validate and apply fantasy_team
            ft = r["fantasy_team"]
            ft = normalize_apostrophes(ft)
            if not ft and r["roster_status"] == "owned" and my_team_name:
                # Yahoo CFB doesn't show fantasy team affiliations in row data
                # Use my_team_name derived from env var for all owned players
                ft = my_team_name
            if ft and not is_valid_team_name(ft):
                print(
                    f"[sync-yahoo] WARN: dropping invalid fantasy_team "
                    f"{ft!r} before history insert for {r['name']}",
                    file=sys.stderr,
                )
                ft = None

            try:
                conn.execute(
                    text("""
                        insert into roster_status_history
                        (league_id, player_id, fantasy_team, roster_status,
                         position, fetched_at, payload)
                        values
                        (:league_id, :player_id, :fantasy_team, :roster_status,
                         :position, :fetched_at, :payload)
                    """),
                    {
                        "league_id": league_id,
                        "player_id": player_id,
                        "fantasy_team": ft,
                        "roster_status": r["roster_status"],
                        "position": r["position"],
                        "fetched_at": fetched_at,
                        "payload": json.dumps(history_payload),
                    },
                )
            except ProgrammingError as e:
                print(
                    f"[yahoo-cfb] ProgrammingError inserting history for "
                    f"player {r['name']}: {e}",
                    file=sys.stderr,
                )
                continue

    print(
        f"[yahoo-cfb] Upserted {len(rows)} rows for "
        f"league_external_id={league_external_id}, "
        f"snapshot fetched_at={fetched_at.isoformat()}",
        flush=True,
    )


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright required: pip install playwright "
            "&& python -m playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    league_ids = get_league_ids()
    if not league_ids:
        print("No YAHOO_LEAGUE_IDS configured; nothing to sync.", file=sys.stderr)
        return

    state_path = resolve_state_path()
    cleanup_temp = YAHOO_STATE_B64 != ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=state_path)
            page = context.new_page()
            page.set_default_timeout(30000)

            for league_id in league_ids:
                print(f"[yahoo-cfb] Syncing league {league_id}", flush=True)
                rows = scrape_all_positions(page, league_id)
                upsert_players_and_history(league_id, rows)

            context.close()
            browser.close()
    finally:
        if cleanup_temp and os.path.exists(state_path):
            os.remove(state_path)


if __name__ == "__main__":
    main()