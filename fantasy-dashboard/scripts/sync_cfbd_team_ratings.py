#!/usr/bin/env python3
"""sync_cfbd_team_ratings.py

Fetch team-level defensive ratings from the CFBD /ratings endpoints and
upsert into cfbd_team_defense_ratings.

Endpoints used (4 API calls — stays well within 30k/mo budget since this
runs weekly):
  1. /ratings/sp      — SP+, includes defense.havoc/passing/rushing/explosiveness/success/rating/ranking
  2. /ratings/fpi     — FPI, includes efficiencies.defense + overall fpi
  3. /ratings/core    — Advanced metrics, includes defense + overall
  4. /ratings/srs     — SRS, includes offense/defense ranking + rating

Also fetches /teams/fbs for conference/division metadata (1 extra call
cached per season in cfbd_player_reference, but we re-pull for team-level).
"""

import os
import sys
import time
import logging
import requests
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [cfbd-ratings] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CFBD_API_KEY = os.getenv("CFBD_API_KEY", "")
CFBD_SEASON = int(os.getenv("CFBD_SEASON", "2026"))
DATABASE_URL = os.getenv("DATABASE_URL", "")

# CFBD v5.27.1 API — all endpoints share the same base with /api/ prefix.
# Ratings endpoints use the "ratings" tag, which is NOT behind the tier-2
# extra-cost threshold.  /ratings/srs *is* tier-2 ($3 per 1k reqs) but we
# only call it once per week, so cost is negligible (<$0.01).
BASE = "https://api.collegefootballdata.com"
HEADERS = {
    "Authorization": f"Bearer {CFBD_API_KEY}",
    "Content-Type": "application/json",
}


def cfbd_get(path: str, params: dict | None = None) -> list:
    """Call CFBD API, retry with exponential backoff on 429/5xx."""
    url = f"{BASE}{path}"
    for attempt in range(4):
        r = requests.get(url, headers=HEADERS, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            delay = 2**attempt + (0.5 * attempt)
            log.warning("  HTTP %d, backing off %.1fs", r.status_code, delay)
            time.sleep(delay)
        else:
            r.raise_for_status()
    raise RuntimeError(f"CFBD {url} failed after retries")


def get_conferences() -> dict[str, str]:
    """Map team -> conference using cfbd_player_reference (already fetched from /teams/fbs)."""
    teams = {}
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT DISTINCT team, conference, division "
                "FROM cfbd_player_reference WHERE team IS NOT NULL"
            )
            for row in cur.fetchall():
                if row["team"]:
                    teams[row["team"]] = {
                        "conference": row["conference"],
                        "division": row["division"],
                    }
    return teams


def fetch_all_ratings() -> list[dict]:
    """Pull ratings from /ratings/sp, /ratings/fpi, /ratings/core, /ratings/srs
    and merge into one dict keyed by team."""
    # 1. SP+ (defense, offense, overall ranking/rating)
    sp = cfbd_get("/ratings/sp", params={"season": CFBD_SEASON, "offense": False})
    sp_by_team = {e["team"]: e for e in sp}

    # 2. FPI (defensive efficiency + overall)
    fpi = cfbd_get("/ratings/fpi", params={"season": CFBD_SEASON})
    fpi_by_team = {e["team"]: e for e in fpi}

    # 3. Core ratings (defense + overall)
    core = cfbd_get("/ratings/core", params={"season": CFBD_SEASON})
    core_by_team = {e["team"]: e for e in core}

    # 4. SRS (offense/defense ranking + rating)
    srs = cfbd_get("/ratings/srs", params={"season": CFBD_SEASON})
    srs_by_team = {e["team"]: e for e in srs}

    conferences = get_conferences()

    merged: list[dict] = []
    all_teams = (
        set(sp_by_team) | set(fpi_by_team) | set(core_by_team) | set(srs_by_team)
    )
    for team in sorted(all_teams):
        e_sp = sp_by_team.get(team, {})
        e_fpi = fpi_by_team.get(team, {})
        e_core = core_by_team.get(team, {})
        e_srs = srs_by_team.get(team, {})

        # SP+ defense sub-object
        sp_def = e_sp.get("defense", {})
        sp_over = e_sp.get("offense", {})  # often absent when offense=False
        sp_off = e_sp.get("overalls", {})  # overall SP+ is sometimes nested

        # FPI defense sub-object
        fpi_eff = e_fpi.get("efficiencies", {})
        fpi_def = fpi_eff.get("defense", {})

        # SRS defense
        srs_def = e_srs.get("defense", {})
        srs_off = e_srs.get("offense", {})

        row = {
            "team": team,
            "season": CFBD_SEASON,
            "conference": (conferences.get(team, {}) or {}).get("conference"),
            "division": (conferences.get(team, {}) or {}).get("division"),
            # SP+
            "sp_defense_ranking": sp.get("ranking"),
            "sp_defense_rating": sp_def.get("rating"),
            "sp_overall_ranking": e_sp.get("overallRanking")
            or e_sp.get("overall_ranking"),
            "sp_overall_rating": e_sp.get("overallRating")
            or e_sp.get("overall_rating"),
            # FPI
            "fpi_defense": fpi_def.get("rating") or fpi_def.get("eppa"),
            "fpi_overall": e_fpi.get("fpi"),
            # SRS
            "srs_defense_ranking": srs.get("ranking"),
            "srs_defense_rating": srs_def.get("rating"),
            "srs_overall_ranking": srs.get("ranking"),
            "srs_overall_rating": srs.get("rating"),
            # Core
            "core_defense": e_core.get("defense"),
            "core_defense_ranking": e_core.get("defenseRank")
            or e_core.get("defense_ranking"),
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S+00:00", time.gmtime()),
        }
        merged.append(row)

    return merged


UPSERT_SQL = """
INSERT INTO cfbd_team_defense_ratings (
    team, season, week, conference, division,
    sp_defense_ranking, sp_defense_rating,
    sp_overall_ranking, sp_overall_rating,
    fpi_defense, fpi_overall,
    srs_defense_ranking, srs_defense_rating,
    srs_overall_ranking, srs_overall_rating,
    core_defense, core_defense_ranking,
    fetched_at
) VALUES (
    %(team)s, %(season)s, %(week)s, %(conference)s, %(division)s,
    %(sp_defense_ranking)s, %(sp_defense_rating)s,
    %(sp_overall_ranking)s, %(sp_overall_rating)s,
    %(fpi_defense)s, %(fpi_overall)s,
    %(srs_defense_ranking)s, %(srs_defense_rating)s,
    %(srs_overall_ranking)s, %(srs_overall_rating)s,
    %(core_defense)s, %(core_defense_ranking)s,
    %(fetched_at)s
)
ON CONFLICT (team, season) DO UPDATE SET
    week = EXCLUDED.week,
    conference = EXCLUDED.conference,
    division = EXCLUDED.division,
    sp_defense_ranking = EXCLUDED.sp_defense_ranking,
    sp_defense_rating = EXCLUDED.sp_defense_rating,
    sp_overall_ranking = EXCLUDED.sp_overall_ranking,
    sp_overall_rating = EXCLUDED.sp_overall_rating,
    fpi_defense = EXCLUDED.fpi_defense,
    fpi_overall = EXCLUDED.fpi_overall,
    srs_defense_ranking = EXCLUDED.srs_defense_ranking,
    srs_defense_rating = EXCLUDED.srs_defense_rating,
    srs_overall_ranking = EXCLUDED.srs_overall_ranking,
    srs_overall_rating = EXCLUDED.srs_overall_rating,
    core_defense = EXCLUDED.core_defense,
    core_defense_ranking = EXCLUDED.core_defense_ranking,
    fetched_at = EXCLUDED.fetched_at
"""


def main():
    log.info("Starting CFBD team defense ratings sync (season=%d)", CFBD_SEASON)

    rows = fetch_all_ratings()
    log.info("  merged %d teams across all rating endpoints", len(rows))

    inserted = 0
    updated = 0
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            for row in rows:
                # Add week key (best effort — use season year as proxy if not available)
                # CFBD ratings are not week-specific for /sp, /fpi; /srs & /core
                # are season-aggregate. We set week to NULL for aggregate, or
                # we could track the latest week from /ratings/srs if it had one.
                row["week"] = None

                try:
                    cur.execute(UPSERT_SQL, row)
                    if cur.rowcount > 0:
                        # rowcount is always 1 (INSERT) or 2 (INSERT + UPDATE via ON CONFLICT)
                        # psycopg3 doesn't distinguish; we'll count as upsert
                        updated += 1
                except Exception as e:
                    log.error("  UPSERT failed for %s: %s", row.get("team"), e)
                    continue

            conn.commit()

    log.info(
        "done: upserted=%d, teams=%d",
        updated,
        len(rows),
    )


if __name__ == "__main__":
    main()
