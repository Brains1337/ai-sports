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
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, DataError

from teams_normalizer import get_def_team

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
    r"^FA$"                          # free agent label
    r"|^[WL]\s*\("                   # "W (Sep 9)" / "L (Sep 9)" game results
    r"|^[\d\s.\-]+$"                 # all-numeric/whitespace garbage
    r"|^Q[1-4]$"                     # quarter tokens: Q1, Q2, Q3, Q4
    r"|^(?:Sat|Sun|Mon|Tue|Wed|Thu|Fri)$"  # day-of-week tokens
    r"|^(?:Final|Live|1st|2nd|3rd|4th)$"   # game status tokens
    r"|^Owned\b"                     # roster status label leaked into team column
    r"|^Owned\s*·"                   # "Owned · Sat" / "Owned · Final" composite
    r"|^Owned\s+\.\s+"               # "Owned . Sat" variant
)

# Regex to extract Yahoo's player key from row HTML/data attributes.
# Yahoo uses player keys like "242.l.37494.pt.1" or "242.p.123456" in data attributes.
YAHOO_PLAYER_KEY_RE = re.compile(r'(?:playerKey|player_key|data-player-key)=["\']([^"\']+)["\']')


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


def get_league_keys() -> list[str]:
    """Parse comma-separated league IDs into a list."""
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
    """Build the Yahoo CFB players page URL for a given position and page offset."""
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


def extract_yahoo_player_key(tr) -> str | None:
    """Extract Yahoo's internal player key from a row element's data attributes or HTML.

    Yahoo embeds player keys in data attributes like data-player-key or data-player-key
    attributes on the row div. The key format varies by page context.
    """
    # Try data attributes on the row element
    for attr in ["data-player-key", "data-playerkey", "data-pkey"]:
        val = tr.get_attribute(attr)
        if val:
            return val

    # Try to find player key from href in name link
    name_link = tr.locator("a.name").first
    if name_link.count() == 0:
        # Try any link that might contain the player key
        links = tr.locator("a").all()
        for link in links:
            href = link.get_attribute("href") or ""
            if "player" in href and ".p." in href:
                # Extract player ID from URL like /cfb/league/37494/player/123456
                m = re.search(r"/player/(\d+)", href)
                if m:
                    return m.group(1)

    # Try inner HTML for playerKey
    inner = tr.inner_html()
    if inner:
        m = YAHOO_PLAYER_KEY_RE.search(inner)
        if m:
            return m.group(1)

    return None


def extract_lineup_slot(row_text: str) -> str | None:
    """Extract roster_status from a Yahoo player row.

    Returns one of: 'owned', 'waivers', 'free_agent' — or None if not recognized.
    Lineup slot (starter/bench) is not determinable from the Yahoo players listing
    page; it defaults to None.
    """
    lowered = row_text.lower()

    # Yahoo CFB renders free agent status as "FA" — not the full word
    # "free agent". Check for both the abbreviation and the full text.
    if "free agent" in lowered or re.search(r"\bfa\b", lowered):
        return "free_agent"

    if "waiver" in lowered:
        return "waivers"

    # All other players are rostered by some team
    return "owned"


def extract_fantasy_team_from_row(row_text: str, row_html: str = "") -> str | None:
    """Extract fantasy team name from a Yahoo player row.

    Yahoo CFB "All Players" page (status=ALL, eteam=ALL) renders an Owner column
    showing the fantasy team name for rostered players, and "FA" for free agents.

    When row_html is available, we attempt to parse the owner cell directly
    for reliability. Otherwise we extract from row_text by finding the text
    that follows the college-team-and-position token.
    """
    lowered = row_text.lower()

    # Free agent — Yahoo renders as "FA" (not "free agent")
    if "free agent" in lowered or re.search(r"\bfa\b", lowered):
        return None

    # Waiver status
    if "waiver" in lowered:
        if MY_TEAM_NAME:
            return normalize_apostrophes(MY_TEAM_NAME)
        return None

    # Try extracting from row_html first (more reliable)
    team = _extract_team_from_html(row_html) if row_html else None
    if team and is_valid_team_name(team):
        return normalize_apostrophes(team)

    # Fall back to text parsing: take text after the TEAM - POS token
    m = TEAM_POS_RE.search(row_text)
    if m:
        remainder = row_text[m.end():].strip()
        if remainder:
            # The remainder may contain trailing status/note text;
            # take the first token as the team name.
            tokens = remainder.split()
            if tokens:
                candidate = " ".join(tokens[:4])  # team names are 1-4 words
                if is_valid_team_name(candidate):
                    return normalize_apostrophes(candidate)
                # Try first two words (some team names are 2 words)
                if len(tokens) >= 2:
                    candidate2 = " ".join(tokens[:2])
                    if is_valid_team_name(candidate2):
                        return normalize_apostrophes(candidate2)
                if len(tokens) >= 3:
                    candidate3 = " ".join(tokens[:3])
                    if is_valid_team_name(candidate3):
                        return normalize_apostrophes(candidate3)

    # Could not determine ownership from this page layout
    return None


def _extract_team_from_html(row_html: str) -> str | None:
    """Parse team name from Yahoo CFB row HTML by locating the owner column.

    Yahoo renders the fantasy team name inside an <a> tag whose href points
    to the team roster page: /cfb/{league_id}/{team_id}.  We look for those
    links directly rather than stripping all tags (game-status tokens like
    "Sat", "Q1", "Final" live in adjacent cells and would otherwise pollute
    the extracted name).
    """
    if not row_html:
        return None

    # Strategy 1: <a href="/cfb/{league_id}/{team_id}">Team Name</a>
    # Team links have a numeric second path segment (small team ID, 1-~64).
    team_link_re = re.compile(
        r'<a\s+(?:[^>]*?\s+)?href="[^"]*/cfb/\d+/\d+[^"]*"'  # href to team page
        r'[^>]*>([^<]+)</a>',
        re.IGNORECASE,
    )
    for m in team_link_re.finditer(row_html):
        name = m.group(1).strip()
        # Strip any nested HTML entities or tags that survived
        name = re.sub(r"<[^>]+>", "", name).strip()
        if name and is_valid_team_name(name):
            return name

    # Strategy 2: data attributes that may encode the owner
    owner_patterns = [
        r'data-team=["\']([^"\']+)["\']',
        r'data-owner=["\']([^"\']+)["\']',
    ]
    for pat in owner_patterns:
        m = re.search(pat, row_html, re.IGNORECASE)
        if m and m.group(1).strip() and is_valid_team_name(m.group(1)):
            return m.group(1).strip()

    # Strategy 3: Strip HTML tags and look for team-like text tokens.
    # This is a last resort for page layouts where the owner cell uses a
    # different href pattern. We skip tokens that look like game-status
    # text (day-of-week, quarter, Final, Live, etc.).
    text = re.sub(r"<[^>]+>", " ", row_html)
    text = re.sub(r"\s+", " ", text).strip()
    m = TEAM_POS_RE.search(text)
    if m:
        remainder = text[m.end():].strip()
        tokens = remainder.split()
        for n in range(1, min(len(tokens) + 1, 5)):
            candidate = " ".join(tokens[:n])
            if is_valid_team_name(candidate):
                return candidate

    return None


def parse_player_rows(page, wanted_pos: str) -> list[dict[str, Any]]:
    """Parse player rows from the Yahoo CFB players page.

    Handles Yahoo CFB's div-based responsive table structure.
    """
    rows: list[dict[str, Any]] = []

    # Yahoo CFB uses a responsive div-based table structure
    trs = page.locator("div.yssf-table-row")
    if trs.count() == 0:
        # Fall back to legacy table structure
        trs = page.locator("table tbody tr")

    total = trs.count()
    if total == 0:
        return rows

    for i in range(total):
        try:
            tr = trs.nth(i)

            # Get player name link
            name_link = tr.locator("a.name").first
            if name_link.count() == 0:
                # Try any link
                name_link = tr.locator("a").first
                if name_link.count() == 0:
                    continue

            name = extract_text(name_link)
            if not name or len(name) < 2:
                continue

            # Get the full row text for analysis
            row_text = extract_text(tr)
            row_html = tr.inner_html() if total < 1000 else ""

            # Try to extract college team and position
            m = TEAM_POS_RE.search(row_text)
            if not m:
                continue
            college_team, pos = m.group(1), m.group(2)
            if pos != wanted_pos:
                continue

            # Verify position matches via player key or name link context
            # Yahoo sometimes shows the same player under multiple positions

            # Extract Yahoo's player key for proper identity tracking
            yahoo_player_key = extract_yahoo_player_key(tr)

            # Extract fantasy team, roster status, and lineup slot
            roster_status = extract_lineup_slot(row_text)
            fantasy_team = extract_fantasy_team_from_row(row_text, row_html)

            note_type = ""
            for phrase in NOTE_PHRASES:
                if phrase in row_text:
                    note_type = phrase
                    break

            rows.append({
                "name": name,
                "college_team": college_team,
                "position": pos,
                "roster_status": roster_status,
                "fantasy_team": fantasy_team,
                "lineup_status": None,
                "note_type": note_type,
                "raw_row_text": row_text,
                "yahoo_player_key": yahoo_player_key,
            })
        except Exception:
            continue

    return rows


def scrape_all_positions(page, league_id: str, max_pages: int = 80, pause: float = 1.0) -> list[dict[str, Any]]:
    """Scrape all player pages for all positions for a given league."""
    all_rows: list[dict[str, Any]] = []

    for pos in POSITIONS:
        seen: set[tuple[str, str]] = set()
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

            rows = parse_player_rows(page, pos)
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


def upsert_players_and_history(league_key: str, rows: list[dict[str, Any]]) -> None:
    """Upsert players and roster history rows for a Yahoo league.

    Args:
        league_key: Yahoo's league key (e.g. "37494" — the numeric league ID)
        rows: Parsed player data from the Yahoo players page
    """
    fetched_at = now()

    with engine.begin() as conn:
        # Look up league by external_league_key (NOT external_league_id).
        # Yahoo's league key is the league ID like "37494" — stored as external_league_key text.
        # The unique constraint leagues_platform_extkey_season_key ensures we match
        # the right league+season combination.
        league_row = conn.execute(
            text(
                "select id from leagues "
                "where external_league_key = :lkey "
                "and platform = :platform "
                "and season = :season"
            ),
            {
                "lkey": league_key,
                "platform": YAHOO_PLATFORM,
                "season": YAHOO_SEASON,
            },
        ).fetchone()

        if league_row is None:
            print(
                f"[yahoo-cfb] No leagues row found for external_league_key={league_key} "
                f"platform={YAHOO_PLATFORM} season={YAHOO_SEASON}; not writing history.",
                file=sys.stderr,
            )
            return

        league_id = league_row[0]

        for r in rows:
            is_def = r["position"] == "DEF"
            def_team = get_def_team(r["college_team"], r["name"]) if is_def else None

            player_payload = {
                "college_team": r["college_team"],
                "note_type": r["note_type"],
                "raw_row_text": r["raw_row_text"],
                "yahoo_player_key": r.get("yahoo_player_key"),
            }
            if def_team:
                player_payload["def_team"] = def_team

            try:
                # Upsert player using external_player_key for Yahoo player ID tracking.
                # Yahoo player keys are alphanumeric (e.g. "242.p.123456"), so we store
                # them in the text column external_player_key, not external_player_id (bigint).
                #
                # Conflict target: ux_players_platform_extkey (unique on platform + external_player_key)
                # Fallback: if yahoo_player_key is None, fall back to name-based matching.
                if r.get("yahoo_player_key"):
                    player_row = conn.execute(
                        text("""
                            insert into players
                                (platform, external_player_key, player_name, pos, sport, payload)
                            values
                                (:platform, :ext_key, :name, :pos, 'NCAAF', :payload)
                            on conflict on constraint ux_players_platform_extkey
                            do update set
                                player_name  = excluded.player_name,
                                pos          = excluded.pos,
                                payload      = players.payload || excluded.payload::jsonb,
                                updated_at   = now()
                            returning id
                        """),
                        {
                            "platform": YAHOO_PLATFORM,
                            "ext_key": r["yahoo_player_key"],
                            "name": r["name"],
                            "pos": r["position"],
                            "payload": json.dumps(player_payload),
                        },
                    ).fetchone()
                else:
                    # No Yahoo player key available — fall back to name-based upsert
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
            except (IntegrityError, DataError) as e:
                print(
                    f"[yahoo-cfb] DB error for player {r['name']} "
                    f"{r['college_team']} {r['position']}: {e}",
                    file=sys.stderr,
                )
                continue

            if player_row is None:
                # Fallback: look up by name if upsert didn't return a row
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

            # Validate fantasy_team — only store real team names, not scraper artifacts
            ft = r["fantasy_team"]
            if ft:
                ft = normalize_apostrophes(ft)
                if not is_valid_team_name(ft):
                    print(
                        f"[sync-yahoo] WARN: dropping invalid fantasy_team "
                        f"{ft!r} before history insert for {r['name']}",
                        file=sys.stderr,
                    )
                    ft = None

            # Build history payload with lineup info if available
            history_payload = {"college_team": r["college_team"]}
            if def_team:
                history_payload["def_team"] = def_team
            if r.get("yahoo_player_key"):
                history_payload["yahoo_player_key"] = r["yahoo_player_key"]

            try:
                conn.execute(
                    text("""
                        insert into roster_status_history
                        (league_id, player_id, fantasy_team, roster_status,
                         position, fetched_at, payload, lineup_status, slot_name)
                        values
                        (:league_id, :player_id, :fantasy_team, :roster_status,
                         :position, :fetched_at, :payload, :lineup_status, :slot_name)
                    """),
                    {
                        "league_id": league_id,
                        "player_id": player_id,
                        "fantasy_team": ft,
                        "roster_status": r["roster_status"],
                        "position": r["position"],
                        "fetched_at": fetched_at,
                        "payload": json.dumps(history_payload),
                        "lineup_status": r.get("lineup_status"),
                        "slot_name": r.get("position"),  # Use NFL position as slot_name
                    },
                )
            except (IntegrityError, DataError) as e:
                print(
                    f"[yahoo-cfb] DB error inserting history for "
                    f"player {r['name']}: {e}",
                    file=sys.stderr,
                )
                continue

    print(
        f"[yahoo-cfb] Upserted {len(rows)} rows for "
        f"league_key={league_key}, "
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

    league_keys = get_league_keys()
    if not league_keys:
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

            for league_key in league_keys:
                print(f"[yahoo-cfb] Syncing league {league_key}", flush=True)
                rows = scrape_all_positions(page, league_key)
                upsert_players_and_history(league_key, rows)

            context.close()
            browser.close()
    finally:
        if cleanup_temp and os.path.exists(state_path):
            os.remove(state_path)


if __name__ == "__main__":
    main()