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

# Platform label must match leagues.platform and what the dashboard expects.
# Your DB row uses 'yahoo-cfb' for Yahoo EDIT League.
YAHOO_PLATFORM = os.getenv("YAHOO_PLATFORM", "yahoo-cfb")

PAGE_SIZE = 25
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]

TEAM_POS_RE = re.compile(r"\b([A-Za-z]{2,6})\s*-\s*(QB|RB|WR|TE|K|DEF)\b")
NOTE_PHRASES = ["No new player Notes", "New Player Note", "Player Note"]

# Unicode apostrophe/quote characters Yahoo renders in team names.
# Normalize all of these to a plain ASCII apostrophe before any DB write.
_APOSTROPHE_CHARS = (
    "\u2019",  # RIGHT SINGLE QUOTATION MARK  '
    "\u2018",  # LEFT SINGLE QUOTATION MARK   '
    "\u02bc",  # MODIFIER LETTER APOSTROPHE   ʼ
    "\u0060",  # GRAVE ACCENT                 `
    "\u00b4",  # ACUTE ACCENT                 ´
)

# Scraper-artifact patterns that must never be stored as a fantasy_team name.
# These arise when the DOM shifts and a non-team element is scraped instead.
#   "W (Sep 9)"  → game result
#   "L (Sep 9)"  → game result
#   "FA"         → free agent label leaking into owned bucket
#   all-numeric  → stats/score row picked up as team name
_INVALID_TEAM_RE = re.compile(
    r"^FA$"           # free agent label
    r"|^[WL]\s*\("    # "W (Sep 9)" / "L (Sep 9)" game results
    r"|^[\d\s.\-]+$"  # all-numeric/whitespace garbage
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_apostrophes(s: str | None) -> str | None:
    """Replace curly/smart apostrophes and look-alikes with a plain ASCII apostrophe.

    Yahoo renders team names with Unicode RIGHT SINGLE QUOTATION MARK (U+2019)
    which never matches the straight apostrophe stored in leagues.my_team_name.
    Apply this to every fantasy_team value before writing to the DB.
    """
    if not s:
        return s
    for ch in _APOSTROPHE_CHARS:
        s = s.replace(ch, "'")
    return s


def is_valid_team_name(name: str | None) -> bool:
    """Return False for scraper artifacts that look like game results, FA labels,
    or numeric stat rows.  Only real fantasy team names (start with a letter,
    length >= 2) are accepted."""
    if not name or len(name.strip()) < 2:
        return False
    return not bool(_INVALID_TEAM_RE.match(name.strip()))


def get_league_ids() -> List[str]:
    raw = YAHOO_LEAGUE_IDS
    return [lid.strip() for lid in raw.split(",") if lid.strip()]


def resolve_state_path() -> str:
    """
    Decode YAHOO_STATE_B64 (from env) into a temp file for Playwright.
    Falls back to YAHOO_STATE_PATH if the b64 var isn't set, for local/manual runs.
    """
    if YAHOO_STATE_B64:
        try:
            raw = base64.b64decode(YAHOO_STATE_B64)
            json.loads(raw)  # sanity check it's valid JSON before writing
        except Exception as e:
            print(
                f"YAHOO_STATE_B64 is set but failed to decode/parse: {e}",
                file=sys.stderr,
            )
            sys.exit(1)

        fd, path = tempfile.mkstemp(prefix="yahoo_state_", suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return path

    if YAHOO_STATE_PATH and os.path.exists(YAHOO_STATE_PATH):
        return YAHOO_STATE_PATH

    print(
        "No Yahoo auth found. Set YAHOO_STATE_B64 in .env "
        "(see encode_yahoo_state.py) or mount a file and set YAHOO_STATE_PATH.",
        file=sys.stderr,
    )
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


def parse_roster_status(row_text: str) -> Tuple[str, str | None]:
    lowered = row_text.lower()
    if "waivers" in lowered:
        return "waivers", None
    if "free agent" in lowered:
        return "free_agent", None
    # Try to extract team name from the row text more broadly
    # Yahoo roster pages often show team info in different formats
    # Look for patterns like "Team [team_name]" or team abbreviations
    m = re.search(r"\b(?:Team\s+)?([A-Z]{2,6})\b", row_text)
    if m:
        team = m.group(1)
        # If it's a valid team abbreviation, return owned status
        if is_valid_team_name(team):
            return "owned", team
        else:
            # For invalid team names, map to free_agent instead of unknown to avoid DB constraint violation
            return "free_agent", None
    
    # If no team pattern found, check if this is a known pattern in Yahoo's data
    # Some Yahoo pages don't explicitly show "Team" text
    if "team" in lowered or "roster" in lowered:
        # Likely a rostered player, so map to owned
        return "owned", None
        
    # For non-matching rows, map to free_agent instead of unknown to avoid DB constraint violation
    return "free_agent", None


def parse_player_rows(page, wanted_pos: str) -> Tuple[List[Dict[str, Any]], int]:
    rows: List[Dict[str, Any]] = []
    trs = page.locator("table tr")
    total = trs.count()
    for i in range(total):
        try:
            tr = trs.nth(i)
            name_link = tr.locator("a.name").first
            if name_link.count() == 0:
                continue
            name = extract_text(name_link)
            if not name or len(name) < 2:
                continue

            row_text = extract_text(tr)
            m = TEAM_POS_RE.search(row_text)
            if not m:
                continue
            college_team, pos = m.group(1), m.group(2)
            if pos != wanted_pos:
                continue

            roster_status, fantasy_team = parse_roster_status(row_text)

            # Belt-and-suspenders: normalize + validate again in case
            # parse_roster_status takes a different code path in the future.
            fantasy_team = normalize_apostrophes(fantasy_team)
            if fantasy_team and not is_valid_team_name(fantasy_team):
                print(
                    f"[sync-yahoo] WARN: discarding invalid fantasy_team "
                    f"{fantasy_team!r} for player {name}",
                    file=sys.stderr,
                )
                fantasy_team = None

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

            rows, _ = parse_player_rows(page, pos)
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
    """
    Infer our own fantasy team name from the scraped rows.

    The MY_TEAM_NAME env var is the authoritative source.  Falling back to the
    most-common 'owned' fantasy_team value is a convenience heuristic — it will
    be correct as long as we own more players than any single opponent.
    """
    explicit = os.getenv("MY_TEAM_NAME", "").strip()
    if explicit:
        return normalize_apostrophes(explicit) or None

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

        # ------------------------------------------------------------------ #
        # Keep my_team_name current in leagues.payload so the waiver planner  #
        # and opponent tracker can always resolve ownership without env vars.  #
        # ------------------------------------------------------------------ #
        if my_team_name:
            # Use consistent SQLAlchemy parameter syntax
            conn.execute(
                text("""
                    update leagues
                    set payload = jsonb_set(
                        coalesce(payload, '{}'::jsonb),
                        :json_path,
                        :my_team_name_json
                    ),
                    updated_at = now()
                    where id = :league_id
                """),
                {
                    "json_path": f"{{my_team_name}}",
                    "my_team_name_json": my_team_name, 
                    "league_id": league_id
                },
            )
            print(
                f"[yahoo-cfb] leagues.payload my_team_name={my_team_name!r} "
                f"for league_id={league_id}",
                flush=True,
            )

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
                # ---------------------------------------------------------- #
                # ON CONFLICT must reference the partial unique index:        #
                #   CREATE UNIQUE INDEX uix_players_platform_name_pos         #
                #       ON players (platform, player_name, pos)               #
                #       WHERE external_player_id IS NULL;                     #
                #                                                             #
                # payload merge uses || so existing keys (e.g.                #
                # cfbd_athlete_id written by sync_cfbd_player_xref.py) are   #
                # preserved — sync_yahoo.py never clobbers xref data.        #
                # ---------------------------------------------------------- #
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
                # Fallback SELECT — must also filter external_player_id IS NULL
                # to target the same partition as the upsert above.
                player_row = conn.execute(
                    text("""
                        select id from players
                        where platform             = :platform
                          and player_name          = :name
                          and pos                  = :pos
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

            # Validate fantasy_team one final time before the history insert.
            ft = r["fantasy_team"]
            ft = normalize_apostrophes(ft)
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
