#!/usr/bin/env python3
"""
waiver_planner.py — hourly recommendation engine

Uses precomputed weekly rankings to generate, per league:
  - Start/sit recommendations for your roster
  - Waiver / pickup targets
  - Drop candidates

Writes to:
  - start_sit_recommendations
  - waiver_targets
  - drop_candidates

Assumptions:
  - `rankings` is already populated for the current week/sport/scoring_type.
  - `roster_status_history` has the latest roster snapshot per player/league.
  - Leagues table has sport + scoring_type + waiver metadata fields.

This is a first-pass implementation; refine SQL joins and slot logic to match
your exact schema once wired.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]

# Minimum improvement (in composite_score) to consider a waiver target
MIN_WAIVER_DELTA = float(os.getenv("MIN_WAIVER_DELTA", "2.0"))
# How many top waiver targets to keep per league
MAX_WAIVER_TARGETS = int(os.getenv("MAX_WAIVER_TARGETS", "10"))
# Minimum gap below replacement to flag a drop candidate
MIN_DROP_DELTA = float(os.getenv("MIN_DROP_DELTA", "1.5"))


def now() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers to load core entities
# ──────────────────────────────────────────────────────────────────────────────


def load_active_leagues(conn) -> Sequence[Mapping[str, Any]]:
    """
    Load leagues we want recommendations for.

    Assumes leagues has: id, league_name, sport, scoring_type, platform, season.
    """
    rows = conn.execute(text("""
        select id,
               league_name,
               sport,
               scoring_type,
               platform,
               season
        from leagues
        where sport is not null
          and scoring_type is not null
        order by id
        """)).mappings().all()
    return rows


def get_current_week(conn, sport: str, scoring_type: str) -> int:
    """
    Determine current week for a given sport/scoring_type by taking the max
    week present in rankings for that combination. Override with FANTASY_WEEK
    env var if set.
    """
    override = os.getenv("FANTASY_WEEK")
    if override:
        try:
            return int(override)
        except ValueError:
            pass

    row = (
        conn.execute(
            text("""
        select max(week) as week
        from rankings
        where sport = :sport
          and scoring_type = :scoring_type
        """),
            {"sport": sport, "scoring_type": scoring_type},
        )
        .mappings()
        .first()
    )
    if not row or row["week"] is None:
        raise RuntimeError(
            f"No rankings found for sport={sport}, scoring_type={scoring_type}"
        )
    return int(row["week"])


def load_rankings_for_league_week(
    conn, sport: str, scoring_type: str, week: int
) -> Dict[int, Mapping[str, Any]]:
    """
    Load rankings rows for this sport/scoring_type/week, keyed by player_id.
    """
    rows = (
        conn.execute(
            text("""
        select player_id,
               proj_pts,
               opp_team,
               def_strength,
               composite_score
        from rankings
        where sport = :sport
          and scoring_type = :scoring_type
          and week = :week
        """),
            {"sport": sport, "scoring_type": scoring_type, "week": week},
        )
        .mappings()
        .all()
    )
    return {int(r["player_id"]): r for r in rows}


def load_starting_spot_count(conn, league_id: int) -> int:
    """
    Approximate total number of starting lineup spots for this league from
    league_slots, excluding bench/IR.

    Assumes league_slots has: league_id, slot_name, slot_count.
    """
    row = (
        conn.execute(
            text("""
        select coalesce(
            sum(slot_count) filter (where slot_name not in ('BE','BN','BENCH','IR','RES')),
            0
        ) as starters
        from league_slots
        where league_id = :league_id
        """),
            {"league_id": league_id},
        )
        .mappings()
        .first()
    )
    return int(row["starters"] or 0)


def load_league_owned_players(
    conn, league_id: int
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Load the latest roster snapshot for all players in a league from
    roster_status_history, and separate:

      - my_roster: players on *any* fantasy_team in this league (first pass)
      - owned_ids: set of player_ids that are currently owned in this league

    NOTE: This does not yet filter to "my" team specifically; that requires
    wiring to your team identity per league. For waiver availability, the union
    of owned players is sufficient. For start/sit, we treat all players with a
    non-null fantasy_team as candidates and will refine later.
    """
    rows = (
        conn.execute(
            text("""
        with latest as (
          select distinct on (player_id)
                 player_id,
                 fantasy_team,
                 roster_status,
                 position,
                 fetched_at
          from roster_status_history
          where league_id = :league_id
          order by player_id, fetched_at desc
        )
        select player_id,
               fantasy_team,
               roster_status,
               position
        from latest
        """),
            {"league_id": league_id},
        )
        .mappings()
        .all()
    )

    my_roster: List[Dict[str, Any]] = []
    owned_ids_set = set()

    for r in rows:
        pid = int(r["player_id"])
        # Consider any player with a fantasy_team as "owned" in this league.
        if r["fantasy_team"]:
            owned_ids_set.add(pid)
            my_roster.append(
                {
                    "player_id": pid,
                    "fantasy_team": r["fantasy_team"],
                    "roster_status": r["roster_status"],
                    "position": r["position"],
                }
            )

    return my_roster, sorted(owned_ids_set)


# ──────────────────────────────────────────────────────────────────────────────
# Recommendation logic
# ──────────────────────────────────────────────────────────────────────────────


def compute_start_sit(
    league_id: int,
    week: int,
    roster: List[Dict[str, Any]],
    rankings: Dict[int, Mapping[str, Any]],
    starter_slots: int,
) -> List[Dict[str, Any]]:
    """
    Very first-pass start/sit: take all rostered players that appear in rankings,
    sort by composite_score, mark top N as 'start' and rest as 'bench'.
    """
    enriched: List[Dict[str, Any]] = []
    for r in roster:
        pid = r["player_id"]
        rank_row = rankings.get(pid)
        if not rank_row:
            continue
        enriched.append(
            {
                "player_id": pid,
                "position": r["position"],
                "composite_score": float(rank_row["composite_score"] or 0.0),
            }
        )

    if not enriched:
        return []

    enriched.sort(key=lambda x: (-x["composite_score"], x["position"] or ""))

    num_starters = min(starter_slots or len(enriched), len(enriched))
    recs: List[Dict[str, Any]] = []

    for idx, row in enumerate(enriched):
        action = "start" if idx < num_starters else "bench"
        recs.append(
            {
                "league_id": league_id,
                "week": week,
                "slot": f"SLOT-{idx+1}",
                "player_id": row["player_id"],
                "composite_score": row["composite_score"],
                "recommended_action": action,
                "rationale": None,
            }
        )

    return recs


def compute_waiver_targets(
    league_id: int,
    week: int,
    rankings: Dict[int, Mapping[str, Any]],
    owned_ids: List[int],
    start_sit_recs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Identify waiver targets as players not currently owned in the league whose
    composite_score significantly exceeds your worst current starter.

    Uses MIN_WAIVER_DELTA and MAX_WAIVER_TARGETS.
    """
    owned_set = set(owned_ids)

    starter_scores = [
        r["composite_score"]
        for r in start_sit_recs
        if r["recommended_action"] == "start"
    ]
    if not starter_scores:
        return []

    worst_starter_score = min(starter_scores)

    candidates: List[Tuple[int, float]] = []
    for pid, r in rankings.items():
        if pid in owned_set:
            continue
        score = float(r["composite_score"] or 0.0)
        delta = score - worst_starter_score
        if delta >= MIN_WAIVER_DELTA:
            candidates.append((pid, score))

    if not candidates:
        return []

    candidates.sort(key=lambda x: -x[1])
    top = candidates[:MAX_WAIVER_TARGETS]

    waiver_recs: List[Dict[str, Any]] = []
    for pid, score in top:
        waiver_recs.append(
            {
                "league_id": league_id,
                "week": week,
                "player_id": pid,
                "projected_pts": float(rankings[pid]["proj_pts"] or 0.0),
                "priority_score": score - worst_starter_score,
                "recommended_drop_player_id": None,
                "status": "open",
                "rationale": None,
            }
        )

    return waiver_recs


def compute_drop_candidates(
    league_id: int,
    week: int,
    roster: List[Dict[str, Any]],
    rankings: Dict[int, Mapping[str, Any]],
    owned_ids: List[int],
) -> List[Dict[str, Any]]:
    """
    Flag drop candidates as rostered players whose composite_score is
    significantly below the average of available free agents at their position.
    """
    owned_set = set(owned_ids)

    # Build available players by position
    free_by_pos: Dict[str, List[float]] = {}
    for pid, r in rankings.items():
        if pid in owned_set:
            continue
        score = float(r["composite_score"] or 0.0)
        # Position not in rankings; relies on players table join below
        # We'll treat position as unknown here; refine later if needed.
        # For now, use a single global replacement pool.
        free_by_pos.setdefault("GLOBAL", []).append(score)

    if not free_by_pos.get("GLOBAL"):
        return []

    avg_replacement = sum(free_by_pos["GLOBAL"]) / len(free_by_pos["GLOBAL"])

    drop_recs: List[Dict[str, Any]] = []
    for r in roster:
        pid = r["player_id"]
        rank_row = rankings.get(pid)
        if not rank_row:
            continue
        score = float(rank_row["composite_score"] or 0.0)
        delta = avg_replacement - score
        if delta >= MIN_DROP_DELTA:
            drop_recs.append(
                {
                    "league_id": league_id,
                    "week": week,
                    "player_id": pid,
                    "composite_score": score,
                    "replacement_delta": delta,
                    "reason_code": "below_replacement",
                    "rationale": None,
                }
            )

    return drop_recs


# ──────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ──────────────────────────────────────────────────────────────────────────────


def persist_start_sit(
    conn, league_id: int, week: int, recs: List[Dict[str, Any]]
) -> None:
    conn.execute(
        text("""
        delete from start_sit_recommendations
        where league_id = :league_id
          and week = :week
        """),
        {"league_id": league_id, "week": week},
    )

    for r in recs:
        conn.execute(
            text("""
        insert into start_sit_recommendations
          (league_id, week, slot, player_id, composite_score, recommended_action, rationale)
        values
          (:league_id, :week, :slot, :player_id, :composite_score, :recommended_action, :rationale)
        """),
            r,
        )


def persist_waiver_targets(
    conn, league_id: int, week: int, recs: List[Dict[str, Any]]
) -> None:
    conn.execute(
        text("""
        delete from waiver_targets
        where league_id = :league_id
          and week = :week
        """),
        {"league_id": league_id, "week": week},
    )

    for r in recs:
        conn.execute(
            text("""
        insert into waiver_targets
          (league_id, week, player_id, projected_pts, priority_score,
           recommended_drop_player_id, status, rationale)
        values
          (:league_id, :week, :player_id, :projected_pts, :priority_score,
           :recommended_drop_player_id, :status, :rationale)
        """),
            r,
        )


def persist_drop_candidates(
    conn, league_id: int, week: int, recs: List[Dict[str, Any]]
) -> None:
    conn.execute(
        text("""
        delete from drop_candidates
        where league_id = :league_id
          and week = :week
        """),
        {"league_id": league_id, "week": week},
    )

    for r in recs:
        conn.execute(
            text("""
        insert into drop_candidates
          (league_id, week, player_id, composite_score, replacement_delta,
           reason_code, rationale)
        values
          (:league_id, :week, :player_id, :composite_score, :replacement_delta,
           :reason_code, :rationale)
        """),
            r,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    summary: List[Dict[str, Any]] = []

    with engine.begin() as conn:
        leagues = load_active_leagues(conn)

        for league in leagues:
            league_id = int(league["id"])
            league_name = league["league_name"]
            sport = league["sport"]
            scoring_type = league["scoring_type"]

            week = get_current_week(conn, sport, scoring_type)
            rankings = load_rankings_for_league_week(conn, sport, scoring_type, week)
            if not rankings:
                continue

            roster, owned_ids = load_league_owned_players(conn, league_id)
            if not roster:
                continue

            starter_slots = load_starting_spot_count(conn, league_id)

            ss_recs = compute_start_sit(
                league_id=league_id,
                week=week,
                roster=roster,
                rankings=rankings,
                starter_slots=starter_slots,
            )
            wt_recs = compute_waiver_targets(
                league_id=league_id,
                week=week,
                rankings=rankings,
                owned_ids=owned_ids,
                start_sit_recs=ss_recs,
            )
            dc_recs = compute_drop_candidates(
                league_id=league_id,
                week=week,
                roster=roster,
                rankings=rankings,
                owned_ids=owned_ids,
            )

            persist_start_sit(conn, league_id, week, ss_recs)
            persist_waiver_targets(conn, league_id, week, wt_recs)
            persist_drop_candidates(conn, league_id, week, dc_recs)

            summary.append(
                {
                    "league_id": league_id,
                    "league_name": league_name,
                    "sport": sport,
                    "scoring_type": scoring_type,
                    "week": week,
                    "roster_size": len(roster),
                    "start_sit_count": len(ss_recs),
                    "waiver_targets_count": len(wt_recs),
                    "drop_candidates_count": len(dc_recs),
                }
            )

    print(json.dumps({"generated_at": now().isoformat(), "leagues": summary}, indent=2))


if __name__ == "__main__":
    main()
