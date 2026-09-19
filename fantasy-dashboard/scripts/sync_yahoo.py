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
import html
import re
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, DataError

from teams_normalizer import get_def_team

DATABASE_URL = os.environ["DATABASE_URL"]


# Support reading YAHOO_STATE_B64 from a .env file when it's too large to
# pass as a Docker env var (ARG_MAX limit). Set YAHOO_STATE_ENV_FILE to the
# path of the .env file.
def _load_state_b64_from_env_file() -> str:
    """Read YAHOO_STATE_B64 from a .env file if YAHOO_STATE_ENV_FILE is set."""
    env_file_path = os.getenv("YAHOO_STATE_ENV_FILE", "")
    if env_file_path and Path(env_file_path).exists():
        content = Path(env_file_path).read_text()
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("YAHOO_STATE_B64="):
                return line.split("=", 1)[1].strip()
    return ""


YAHOO_STATE_B64 = os.getenv("YAHOO_STATE_B64", "")
if not YAHOO_STATE_B64:
    YAHOO_STATE_B64 = _load_state_b64_from_env_file()

# Support multiple Yahoo CFB leagues; comma-separated IDs.
# Example: YAHOO_LEAGUE_IDS=37494,12345
YAHOO_LEAGUE_IDS = os.getenv("YAHOO_LEAGUE_IDS", os.getenv("YAHOO_LEAGUE_ID", "37494"))
YAHOO_SEASON = int(os.getenv("YAHOO_SEASON", "2026"))
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
    r"^FA$"  # free agent label
    r"|^Free\s+agent$"  # "Free agent" display text
    r"|^Free$"  # "Free" token
    r"|^[WL]\s*\("  # "W (Sep 9)" / "L (Sep 9)" game results
    r"|^[\d\s.\-]+$"  # all-numeric/whitespace garbage
    r"|^Q[1-4]$"  # quarter tokens: Q1, Q2, Q3, Q4
    r"\|^(?:Sat|Sun|Mon|Tue|Wed|Thu|Fri)$"  # day-of-week tokens
    r"\|^(?:Final|Live|1st|2nd|3rd|4th)$"  # game status tokens
    r"|^Owned\b"  # roster status label leaked into team column
    r"|^Owned\s*\u00b7"  # "Owned · Sat" / "Owned · Final" composite
    r"|^Owned\s+\.\s+"  # "Owned . Sat" variant
)

# Regex to extract Yahoo's player key from row HTML/data attributes.
# Yahoo uses player keys like "242.l.37494.pt.1" or "242.p.123456" in data attributes.
YAHOO_PLAYER_KEY_RE = re.compile(
    r'(?:playerKey|player_key|data-player-key)=[\"\']([^\"\']+)[\"\']'
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
    stripped = name.strip()
    if stripped[0].isdigit():
        return False
    if _INVALID_TEAM_RE.match(stripped):
        return False
    # Reject multi-token candidates that contain any game-status token.
    # A real team name like "Darth Gator" will never contain "Sat", "Q1",
    # "6:45", "21-14", etc. A leaked candidate like "Sat 6:45" must be rejected.
    for tok in stripped.split():
        if is_game_status_token(tok):
            return False
    return True


# Game-status tokens that appear in Yahoo's row text but are NOT team names.
# These may appear before or interleaved with the team name in the flat text
# after the TEAM - POS pattern.
_GAME_STATUS_TOKENS = {
    "sat",
    "sun",
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "final",
    "live",
    "am",
    "pm",
    "1st",
    "2nd",
    "3rd",
    "4th",
}


def is_game_status_token(token: str) -> bool:
    """Return True if a token is a game-status indicator, not a team name."""
    stripped = token.strip(".,;:")
    lower = stripped.lower()
    if lower in _GAME_STATUS_TOKENS:
        return True
    if re.match(r"^Q[1-4]$", stripped):
        return True
    # Time values like "6:45", "14:10" — game clock/schedule tokens
    if re.match(r"^\d{1,2}:\d{2}$", stripped):
        return True
    # Game scores like "21-14", "31-21"
    if re.match(r"^\d+-\d+$", stripped):
        return True
    # "@" or "vs" — game location indicators
    if stripped in ("@", "vs", "VS"):
        return True
    # Ordinal period markers
    if re.match(r"^\d+(?:st|nd|rd|th)$", lower):
        return True
    return False


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

    print(
        "No Yahoo auth found. Set YAHOO_STATE_B64 or YAHOO_STATE_PATH.",
        file=sys.stderr,
    )
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
            # First, try to find "Owned · TeamName" pattern. Yahoo renders
            # owned players as "Owned · TeamName" in the Status column.
            # This is the most reliable text-based signal for the team name.
            owned_match = re.search(
                r"Owned\s*\u00b7\s*(.+)",
                remainder,
            )
            if owned_match:
                candidate = owned_match.group(1).strip()
                # Clean up trailing game-status and schedule artifacts
                # that may appear after the team name in the row text
                # (e.g. "Darth Gator vs Sat 6:45 PM")
                tokens = candidate.split()
                team_tokens = []
                for tok in tokens:
                    if is_game_status_token(tok):
                        break
                    if re.match(r"^[A-Z]{2,5}$", tok):
                        # Opponent abbreviation — stop, game info starts
                        break
                    if tok in ("Owned", "Free", "agent", "Free agent"):
                        continue
                    team_tokens.append(tok)
                candidate = " ".join(team_tokens)
                if is_valid_team_name(candidate):
                    return normalize_apostrophes(candidate)

            tokens = remainder.split()
            # Iterate through tokens, accumulating a candidate name. Skip
            # game-status tokens (Sat, Sun, Q1, Final, etc.) that appear
            # before or interleaved with the team name in the flat text.
            candidate_tokens = []
            for tok in tokens:
                # Skip game-status tokens entirely (never part of a team name)
                if is_game_status_token(tok):
                    continue
                # Skip opponent abbreviations (2-5 letter college codes) that
                # appear in the game schedule portion of the row text
                if re.match(r"^[A-Z]{2,5}$", tok):
                    continue
                # Skip standalone "Owned" that appears before the "·" separator
                if tok in ("Owned", "Free", "agent"):
                    continue
                candidate_tokens.append(tok)
                candidate = " ".join(candidate_tokens[:4])
                if is_valid_team_name(candidate):
                    return normalize_apostrophes(candidate)
                if len(candidate_tokens) >= 4:
                    break

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
    # Owner column links point to team roster pages. Must exclude Yahoo
    # action links like /proposetrade, /addplayer, /addplayerwatch,
    # /pointsagainst whose href also contains /cfb/{league_id}/{numeric-id}/...
    _ACTION_WORDS = {
        "proposetrade",
        "addplayer",
        "addplayerwatch",
        "pointsagainst",
        "watchlist",
        "trade",
        "move",
        "drop",
        "add",
    }
    team_link_re = re.compile(
        r'<a\s+(?:[^>]*?\s+)?href="[^"]*/cfb/\d+/(\d+)(/|\b)([^"]*)"'
        r"[^>]*>([^<]+)</a>",
        re.IGNORECASE,
    )
    for m in team_link_re.finditer(row_html):
        team_id_str = m.group(1)
        rest_of_path = m.group(3)
        name = m.group(4).strip()
        # After /cfb/{league_id}/{team_id} the path should be empty or a
        # team-page suffix. Action links have /proposetrade, /addplayer?...
        # Skip any link whose path after the team ID is an action word.
        path_after_team = rest_of_path.lower().split("?")[0].split("/")[0]
        if path_after_team in _ACTION_WORDS:
            continue
        # Decode HTML entities like &#39; -> ' before validating
        name = html.unescape(name).strip()
        name = re.sub(r"<[^>]+>", "", name).strip()
        if name and is_valid_team_name(name):
            return name

    # Strategy 2: data attributes that may encode the owner
    owner_patterns = [
        r'data-team=[\'"]([^\'"]+)[\'"]',
        r'data-owner=[\'"]([^\'"]+)[\'"]',
    ]
    for pat in owner_patterns:
        m = re.search(pat, row_html, re.IGNORECASE)
        if m and m.group(1).strip() and is_valid_team_name(m.group(1)):
            return m.group(1).strip()

    # Strategy 3: Strip HTML tags and look for team-like text tokens.
    text = re.sub(r"<[^>]+>", " ", row_html)
    text = re.sub(r"\s+", " ", text).strip()
    # First try: find "Owned · TeamName" pattern in stripped text
    owned_match = re.search(r"Owned\s*\u00b7\s*(.+)", text)
    if owned_match:
        team_candidate = owned_match.group(1).strip()
        tokens = team_candidate.split()
        team_tokens = []
        for tok in tokens:
            if is_game_status_token(tok):
                break
            if re.match(r"^[A-Z]{2,5}$", tok):
                break
            if tok == "Owned":
                continue
            if re.match(r"^\d{1,2}:\d{2}\s*(am|pm)$", tok, re.I):
                break
            if re.match(r"^(Final|W|L|T|OT|FINAL)$", tok, re.I):
                break
            if re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*$", tok):
                team_tokens.append(tok)
            else:
                # Non-alpha token after team name likely means we've
                # passed the team name into game-schedule text
                if team_tokens:
                    break
                continue
        candidate = " ".join(team_tokens[:4])
        if candidate and is_valid_team_name(candidate):
            return candidate
    # Fallback: use TEAM_POS_RE to find remainder and accumulate tokens
    m = TEAM_POS_RE.search(text)
    if m:
        remainder = text[m.end():].strip()
        tokens = remainder.split()
        candidate_tokens = []
        for tok in tokens:
            if is_game_status_token(tok):
                continue
            if re.match(r"^[A-Z]{2,5}$", tok):
                continue
            if tok in ("Owned", "Free", "agent"):
                continue
            candidate_tokens.append(tok)
            candidate = " ".join(candidate_tokens[:4])
            if is_valid_team_name(candidate):
                return candidate
            if len(candidate_tokens) >= 4:
                break

    return None


def parse_player_rows(page, wanted_pos: str) -> list[dict[str, Any]]:
    """Parse player rows from the Yahoo CFB players page.

    Handles Yahoo CFB's div-based responsive table structure.
    """
    rows: list[dict[str, Any]] = []

    # Yahoo CFB uses a responsive div-based table structure
    # Try multiple selectors for different Yahoo page versions
    trs = page.locator("div.yssf-table-row")
    if trs.count() == 0:
        # Try alternate Y2/Yahoo 2026 selectors
        trs = page.locator("div[data-test-locator='player-row'], div[class*='PlayerRow'], div[class*='player-row']")
    if trs.count() == 0:
        # Fall back to legacy table structure
        trs = page.locator("table tbody tr")
    if trs.count() == 0:
        # Last resort: any div with player-like content
        trs = page.locator("div[data-player-key], div[data-playerkey]")

    total = trs.count()
    if total == 0:
        # Debug: dump page HTML so we can see Yahoo's actual DOM structure
        import time as _time
        _time.sleep(2)  # wait a bit longer for any async render
        _html = page.content()
        _ts = _time.strftime("%Y%m%d_%H%M%S")
        _debug_path = os.path.join(
            os.path.dirname(__file__), f"yahoo_page_debug_{wanted_pos}_{_ts}.html"
        )
        with open(_debug_path, "w", encoding="utf-8") as _f:
            _f.write(_html)
        print(f"  [DEBUG] No rows found for {wanted_pos}. Page HTML saved to {_debug_path}", flush=True)
        # Also dump a snippet of element tags to see what's there
        _tags = re.findall(r"<([a-zA-Z][a-zA-Z0-9-]*)[^>]*>", _html[:50000])
        from collections import Counter as _Counter
        _top_tags = _Counter(_tags).most_common(10)
        print(f"  [DEBUG] Top tags in page head: {_top_tags}", flush=True)
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

            rows.append(
                {
                    "name": name,
                    "college_team": college_team,
                    "position": pos,
                    "roster_status": roster_status,
                    "fantasy_team": fantasy_team,
                    "lineup_status": None,
                    "note_type": note_type,
                    "raw_row_text": row_text,
                    "yahoo_player_key": yahoo_player_key,
                }
            )
        except Exception:
            continue

    return rows


def scrape_all_positions(
    page, league_id: str, max_pages: int = 80, pause: float = 1.0
) -> list[dict[str, Any]]:
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
            history_payload = {
                "college_team": r["college_team"],
                "player_name": r["name"],
            }
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
    return len(rows)


def sync_yahoo_roster_assignments(league_key: str) -> int:
    """Populate roster_assignments from the latest roster_status_history snapshot.

    For each player in the latest roster_status_history snapshot for this league,
    look up:
      - member_id from leagues_members (by league_id + fantasy_team)
      - athlete_id from cfbd_player_reference (matched on normalized_name +
        normalized_team + season)

    Then upsert into roster_assignments: if the player's current assignment
    matches (same athlete_id/member_id, valid_to IS NULL), skip. If changed,
    set valid_to on the old row and insert a new row.

    This CFBD athlete_id linking is done here in sync_yahoo.py rather than in
    a separate script — sync_yahoo.py scrapes Yahoo, upserts into players +
    roster_status_history, matches to cfbd_player_reference by (name, college_team),
    and writes roster_assignments — so the full Yahoo->CFBD->roster_assignments
    pipeline runs in one place. Requires leagues_members to be populated first
    (by sync_yahoo_members.py's member sync phase).
    """
    fetched_at = now()
    with engine.begin() as conn:
        league_row = conn.execute(
            text(
                "select id, season from leagues "
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
                f"[yahoo-cfb] No leagues row for external_league_key={league_key} "
                f"platform={YAHOO_PLATFORM} season={YAHOO_SEASON}; "
                f"skipping roster_assignments.",
                file=sys.stderr,
            )
            return 0

        league_id = league_row[0]
        season = league_row[1]

        # Get the latest roster_status_history snapshot for this league,
        # join to leagues_members and cfbd_player_reference to resolve
        # member_id and athlete_id.
        rows = (
            conn.execute(
                text("""
                with latest as (
                    select distinct on (player_id)
                        player_id, league_id, fantasy_team,
                        roster_status, lineup_status, slot_name,
                        payload
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
                    lms.waiver_priority,
                    rsh.player_id,
                    (cpr.athlete_id)::text as athlete_id,
                    rsh.roster_status,
                    rsh.lineup_status,
                    rsh.slot_name,
                    rsh.payload->>'player_name' as player_name,
                    rsh.payload->>'college_team' as college_team
                from latest rsh
                join leagues l on l.id = rsh.league_id
                join leagues_members lms on lms.league_id = l.id
                    and lms.fantasy_team = rsh.fantasy_team
                left join cfbd_player_reference cpr
                    on cpr.normalized_name =
                        lower(regexp_replace(rsh.payload->>'player_name', '[^a-zA-Z0-9]', '', 'g'))
                    and cpr.normalized_team =
                        lower(regexp_replace(rsh.payload->>'college_team', '[^a-zA-Z0-9]', '', 'g'))
                    and cpr.season = l.season
                where lms.id is not null
            """),
                {"league_id": league_id},
            )
            .mappings()
            .all()
        )

        inserted = 0
        for row in rows:
            athlete_id = row["athlete_id"]
            player_id = row["player_id"]
            member_id = row["member_id"]
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
                    "league_id": league_id,
                    "member_id": member_id,
                    "athlete_id": athlete_id,
                    "player_id": player_id,
                },
            ).fetchone()

            if existing:
                # Already assigned — check if roster_status or athlete_id changed.
                # athlete_id may have been populated since the last snapshot
                # (e.g., after a CFBD cross-reference run).
                updates = {
                    "id": existing[0],
                    "roster_status": row["roster_status"],
                    "lineup_status": row["lineup_status"],
                    "slot_name": row["slot_name"],
                }
                if athlete_id is not None:
                    sql = text("""
                        update roster_assignments
                        set roster_status = :roster_status,
                            lineup_status = :lineup_status,
                            slot_name = :slot_name,
                            athlete_id = :athlete_id,
                            fetched_at = now()
                        where id = :id
                    """)
                    updates["athlete_id"] = athlete_id
                else:
                    sql = text("""
                        update roster_assignments
                        set roster_status = :roster_status,
                            lineup_status = :lineup_status,
                            slot_name = :slot_name,
                            fetched_at = now()
                        where id = :id
                    """)
                conn.execute(sql, updates)
                continue

            # Check if this athlete was previously assigned to someone else
            conn.execute(
                text("""
                    update roster_assignments
                    set valid_to = now()
                    where league_id = :league_id
                      and (athlete_id = :athlete_id or player_id = :player_id)
                      and valid_to is null
                """),
                {
                    "league_id": league_id,
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
                    "league_id": league_id,
                    "member_id": member_id,
                    "athlete_id": athlete_id,
                    "player_id": player_id,
                    "roster_status": row["roster_status"],
                    "lineup_status": row["lineup_status"],
                    "slot_name": row["slot_name"],
                    "source_name": "sync_yahoo",
                    "season": season,
                    "sport": row["sport"],
                    "fetched_at": fetched_at,
                    "payload": json.dumps({"fantasy_team": fantasy_team}),
                },
            )
            inserted += 1

        print(
            f"[yahoo-cfb] league_key={league_key}: "
            f"upserted {inserted} roster_assignments",
            flush=True,
        )
        return inserted


def _anti_detection_script() -> str:
    """Return JavaScript to inject into every page to hide headless detection.

    Yahoo's 2026 anti-bot system checks for:
    - navigator.webdriver property
    - plugins / mimeTypes arrays
    - languages override
    - chrome runtime object presence
    - permissions.query() override
    """
    return """
    // Overwrite navigator.webdriver to undefined (headless detection bypass)
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Mock plugins array (headless Chrome has empty plugins)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { filename: 'chrome.pdf.dll', filename: 'Chrome PDF Plugin' },
            { filename: 'internal-pdf-viewer', filename: 'Chrome PDF Viewer' },
            { filename: 'pdfviewer', filename: 'PDF Viewer' },
        ],
    });

    // Mock mimeTypes
    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => [
            { type: 'application/pdf', suffixes: 'pdf', description: 'PDF' },
            { type: 'application/pdf', suffixes: 'pdf', description: 'PDF' },
        ],
    });

    // Force languages to a standard en-US value
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    // Chrome runtime object (real Chrome has it, headless doesn't)
    if (!window.chrome) {
        window.chrome = { runtime: {} };
    }

    // Override permissions.query to always return 'granted' for common permissions
    const originalQuery = navigator.permissions && navigator.permissions.query;
    if (originalQuery) {
        navigator.permissions.query = (params) =>
            Promise.resolve({ state: 'granted' });
    }
    """


def _interactive_login(page) -> bool:
    """Perform interactive Yahoo login on the given page.

    Mirrors auto_yahoo_state.py's login flow. Returns True if login
    appears successful, False if still on the login page (2FA/MFA).
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    yahoo_user = os.getenv("YAHOO_USERNAME", "")
    yahoo_pass = os.getenv("YAHOO_PASSWORD", "")

    # If no username/password, try .env file
    if not yahoo_user or not yahoo_pass:
        env_file = os.getenv("YAHOO_STATE_ENV_FILE", "/app/.env")
        if Path(env_file).exists():
            for line in Path(env_file).read_text().splitlines():
                line = line.strip()
                if line.startswith("YAHOO_USERNAME="):
                    yahoo_user = line.split("=", 1)[1].strip()
                elif line.startswith("YAHOO_PASSWORD="):
                    yahoo_pass = line.split("=", 1)[1].strip()

    if not yahoo_user or not yahoo_pass:
        print(
            "[yahoo-cfb] No YAHOO_USERNAME/YAHOO_PASSWORD for interactive login.",
            file=sys.stderr,
        )
        return False

    print(f"[yahoo-cfb] Interactive login as {yahoo_user}...", flush=True)

    # Yahoo's 2026 login page renders via Next.js client-side
    login_urls = [
        "https://login.yahoo.com/",
        "https://login.yahoo.com/account/login",
    ]
    for login_url in login_urls:
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)  # JS hydration
            break
        except Exception:
            continue

    # Fill username
    username_found = False
    for selector in ['input[name="username"]', "#login-username", ".phone_id"]:
        try:
            page.wait_for_selector(selector, timeout=15000)
            page.fill(selector, yahoo_user, timeout=30000)
            username_found = True
            break
        except PlaywrightTimeoutError:
            pass

    if not username_found:
        print("[yahoo-cfb] Could not find username field for login.", file=sys.stderr)
        return False

    # Click "Next"
    for selector in ["button[name='signin']", "input#login-signup", "button[name='next']"]:
        try:
            page.click(selector, timeout=10000)
            break
        except PlaywrightTimeoutError:
            pass
    page.wait_for_timeout(2000)

    # Fill password
    for selector in ['input[name="password"]', "#login-passwrd"]:
        try:
            page.fill(selector, yahoo_pass, timeout=60000)
            break
        except PlaywrightTimeoutError:
            pass

    # Click "Sign In"
    for selector in ["button[name='validate']", "button[name='signin']", "input#login-signup", "button[type='submit']"]:
        try:
            page.click(selector, timeout=10000)
            break
        except PlaywrightTimeoutError:
            pass

    # Wait for login to complete
    page.wait_for_timeout(8000)

    # Check if we're still on login page (2FA/MFA)
    try:
        page.wait_for_selector('input[name="username"]', timeout=3000)
        print(
            "[yahoo-cfb] Still on login page — 2FA/MFA required. Cannot proceed.",
            file=sys.stderr,
        )
        return False
    except PlaywrightTimeoutError:
        pass  # Login proceeded past the username screen

    return True


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
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
            # Launch Chromium with anti-detection flags
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            context = browser.new_context(
                storage_state=state_path,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Sec-Ch-Ua": '"Chromium";v="120", "Not:A-BRACK", "Not?3Q1"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                    "Referer": "https://college.fantasysports.yahoo.com/",
                },
            )

            # Inject anti-detection JavaScript before any page loads
            context.add_init_script(_anti_detection_script())

            page = context.new_page()
            page.set_default_timeout(30000)

            # Try with stored state first. If Yahoo's anti-bot challenge
            # triggers (server-side block), fall back to interactive login.
            use_interactive = False
            if state_path:
                # Quick test: visit homepage to check if auth is still valid
                print("[yahoo-cfb] Warming up session (visiting www.yahoo.com)...",
                      flush=True)
                page.goto("https://www.yahoo.com", wait_until="domcontentloaded",
                         timeout=60000)
                page.wait_for_timeout(3000)
                content_preview = page.content()[:10000]
                if "challenge" in page.url or "challenge" in content_preview:
                    print(
                        "[yahoo-cfb] Stored state rejected by Yahoo anti-bot. "
                        "Falling back to interactive login...",
                        file=sys.stderr,
                        flush=True,
                    )
                    use_interactive = True
                else:
                    print("[yahoo-cfb] Stored state valid, proceeding with scrape.",
                          flush=True)
            else:
                use_interactive = True

            if use_interactive:
                if not _interactive_login(page):
                    print(
                        "[yahoo-cfb] Interactive login failed. Cannot sync.",
                        file=sys.stderr,
                    )
                    return

                # Save refreshed storage state back to .env
                try:
                    _state_path = tempfile.mkstemp(suffix=".json")[1]
                    context.storage_state(path=_state_path)
                    raw_state = Path(_state_path).read_bytes()
                    encoded = base64.b64encode(raw_state).decode("ascii")
                    Path(_state_path).unlink()
                    env_file = Path(os.getenv("YAHOO_STATE_ENV_FILE", "/app/.env"))
                    if env_file.exists():
                        content = env_file.read_text()
                        if "YAHOO_STATE_B64=" in content:
                            content = re.sub(
                                r'^YAHOO_STATE_B64=.*$',
                                f"YAHOO_STATE_B64={encoded}",
                                content,
                                flags=re.MULTILINE,
                            )
                        env_file.write_text(content)
                        print(f"[yahoo-cfb] Refreshed YAHOO_STATE_B64 in {env_file}",
                              flush=True)
                except Exception as e:
                    print(f"[yahoo-cfb] WARN: Could not refresh .env state: {e}",
                          file=sys.stderr)

            for league_key in league_keys:
                print(f"[yahoo-cfb] Syncing league {league_key}", flush=True)
                rows = scrape_all_positions(page, league_key)
                upsert_players_and_history(league_key, rows)
                sync_yahoo_roster_assignments(league_key)

            context.close()
            browser.close()
    finally:
        if cleanup_temp and os.path.exists(state_path):
            os.remove(state_path)


if __name__ == "__main__":
    main()
