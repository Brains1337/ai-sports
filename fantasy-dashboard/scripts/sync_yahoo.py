#!/usr/bin/env python3
"""
sync_yahoo.py — Season-long Yahoo college fantasy roster sync.

Scrapes status=ALL (every player: rostered + free agent + waivers) for each
position, upserts players into the shared `players` table, and inserts a
snapshot row per player into `roster_status_history` for season-long tracking
of adds/drops/trades.

Auth: the Yahoo Playwright storage_state is read from the YAHOO_STATE_B64
env var (base64-encoded JSON), which is set via .env / your GitLab CI/CD
variables and rsync'd deployment — no loose session file needed on disk.
Use encode_yahoo_state.py to produce that value once, then refresh it
whenever the Yahoo session expires.

Requires: playwright, sqlalchemy, psycopg[binary]
One-time setup: python -m playwright install --with-deps chromium
"""
import base64
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
YAHOO_LEAGUE_ID = os.getenv("YAHOO_LEAGUE_ID", "37494")
YAHOO_SEASON = int(os.getenv("YAHOO_SEASON", "2026"))
YAHOO_STATE_B64 = os.getenv("YAHOO_STATE_B64", "")
YAHOO_STATE_PATH = os.getenv("YAHOO_STATE_PATH", "")  # optional fallback: mounted file
PAGE_SIZE = 25
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]

TEAM_POS_RE = re.compile(r'\b([A-Za-z]{2,6})\s*-\s*(QB|RB|WR|TE|K|DEF)\b')
NOTE_PHRASES = ["No new player Notes", "New Player Note", "Player Note"]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def now():
    return datetime.now(timezone.utc)


def resolve_state_path():
    """Decode YAHOO_STATE_B64 (from .env) into a temp file for Playwright.
    Falls back to YAHOO_STATE_PATH if the b64 var isn't set, for local/manual runs."""
    if YAHOO_STATE_B64:
        try:
            raw = base64.b64decode(YAHOO_STATE_B64)
            json.loads(raw)  # sanity check it's valid JSON before writing
        except Exception as e:
            print(f"YAHOO_STATE_B64 is set but failed to decode/parse: {e}", file=sys.stderr)
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


def build_url(pos, start):
    params = {
        "status": "ALL",     # ALL players: rostered + free agent + waivers
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
    return f"https://college.fantasysports.yahoo.com/cfb/{YAHOO_LEAGUE_ID}/players?{urlencode(params)}"


def extract_text(node):
    try:
        return " ".join(node.inner_text().split())
    except Exception:
        return ""


def parse_roster_status(row_text):
    lowered = row_text.lower()
    if "waivers" in lowered:
        return "waivers", None
    if "free agent" in lowered:
        return "free_agent", None
    m = re.search(r'\bTeam\s+([A-Za-z0-9 .\'-]{2,30})', row_text)
    if m:
        return "owned", m.group(1).strip()
    return "unknown", None


def parse_player_rows(page, wanted_pos):
    rows = []
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
                "note_type": note_type,
                "raw_row_text": row_text,
            })
        except Exception:
            continue
    return rows, total


def scrape_all_positions(page, max_pages=80, pause=1.0):
    all_rows = []
    for pos in POSITIONS:
        seen = set()
        start = 0
        empty_streak = 0
        for page_no in range(1, max_pages + 1):
            url = build_url(pos, start)
            print(f"[{pos}] page {page_no} (count={start})", flush=True)
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

            print(f"[{pos}] matched={len(rows)} new={added}", flush=True)

            if len(rows) == 0:
                empty_streak += 1
            else:
                empty_streak = 0
            if empty_streak >= 2 or (added == 0 and page_no > 1) or (len(rows) < PAGE_SIZE and page_no > 1):
                break
            start += PAGE_SIZE
    return all_rows


def upsert_players_and_history(rows):
    fetched_at = now()
    with engine.begin() as conn:
        league_row = conn.execute(
            text("select id from leagues where external_league_id = :lid and platform = 'yahoo_college'"),
            {"lid": int(YAHOO_LEAGUE_ID)},
        ).fetchone()
        league_id = league_row[0] if league_row else None

        for r in rows:
            player_row = conn.execute(
                text("""
                    insert into players (platform, external_player_id, player_name, pos, payload)
                    values (:platform, null, :name, :pos, :payload)
                    on conflict (platform, external_player_id) do nothing
                    returning id
                """),
                {
                    "platform": "yahoo_college",
                    "name": r["name"],
                    "pos": r["position"],
                    "payload": {
                        "college_team": r["college_team"],
                        "note_type": r["note_type"],
                        "raw_row_text": r["raw_row_text"],
                    },
                },
            ).fetchone()

            if player_row is None:
                player_row = conn.execute(
                    text("""
                        select id from players
                        where platform = 'yahoo_college' and player_name = :name and pos = :pos
                    """),
                    {"name": r["name"], "pos": r["position"]},
                ).fetchone()

            if player_row is None:
                continue
            player_id = player_row[0]

            conn.execute(
                text("""
                    insert into roster_status_history
                        (league_id, player_id, fantasy_team, roster_status, position, fetched_at, payload)
                    values
                        (:league_id, :player_id, :fantasy_team, :roster_status, :position, :fetched_at, :payload)
                """),
                {
                    "league_id": league_id,
                    "player_id": player_id,
                    "fantasy_team": r["fantasy_team"],
                    "roster_status": r["roster_status"],
                    "position": r["position"],
                    "fetched_at": fetched_at,
                    "payload": {"college_team": r["college_team"]},
                },
            )
    print(f"Upserted {len(rows)} rows, snapshot fetched_at={fetched_at.isoformat()}", flush=True)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright required: pip install playwright && python -m playwright install chromium", file=sys.stderr)
        sys.exit(1)

    state_path = resolve_state_path()
    cleanup_temp = YAHOO_STATE_B64 != ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=state_path)
            page = context.new_page()
            page.set_default_timeout(30000)

            rows = scrape_all_positions(page)

            context.close()
            browser.close()
    finally:
        if cleanup_temp and os.path.exists(state_path):
            os.remove(state_path)

    upsert_players_and_history(rows)


if __name__ == "__main__":
    main()
