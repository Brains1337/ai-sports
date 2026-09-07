#!/usr/bin/env python3
"""
waiver_planner.py — hourly recommendation engine

Uses precomputed weekly rankings (in `rankings`) to generate, per league:
  - Start/sit recommendations for YOUR roster (my_team_name)
  - Waiver / pickup targets
  - Drop candidates

Writes to:
  - start_sit_recommendations
  - waiver_targets
  - drop_candidates

Assumptions:
  - `rankings` is populated for the current week/sport/scoring_type.
  - `roster_status_history` has the latest roster snapshot per player/league.
  - `leagues` has: sport, scoring_type, platform, my_team_name.
"""

import json
import os
from collections import defaultdict
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

    We skip non-roster formats like ESPN pick'em.
    """
    rows = conn.execute(text("""
        select id,
               league_name,
               sport,
               scoring_type,
               platform,
               season,
               my_team_name
        from leagues
        where platform not in ('espn-pickem')
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


def load_players_meta(conn) -> Dict[int, str]:
    """
    Load base position per player from players table.
    """
    rows = conn.execute(text("""
        select id, pos
        from players
        """)).mappings().all()
    meta: Dict[int, str] = {}
    for r in rows:
        pid = int(r["id"])
        pos = (r["pos"] or "").upper()
        if pos == "DST":
            pos = "D/ST"
        meta[pid] = pos
    return meta


def load_slot_configuration(
    conn, league_id: int
) -> Tuple[Dict[str, int], List[Dict[str, Any]], int]:
    """
    From league_slots, derive:
      - base_slots: required starters per base position (QB/RB/WR/TE/K/D/ST)
      - flex_slots: list of flex slot specs {allowed: [positions], count: int}
      - total_starters: total number of non-bench slots
    """
    rows = (
        conn.execute(
            text("""
        select slot_name, slot_count
        from league_slots
        where league_id = :league_id
        """),
            {"league_id": league_id},
        )
        .mappings()
        .all()
    )

    base_slots: Dict[str, int] = defaultdict(int)
    flex_slots: List[Dict[str, Any]] = []
    total_starters = 0

    for r in rows:
        name = (r["slot_name"] or "").upper()
        count = int(r["slot_count"] or 0)

        if name in ("BE", "BN", "BENCH", "IR", "RES"):
            continue

        if name in ("QB", "RB", "WR", "TE", "K", "D/ST", "DST"):
            pos = "D/ST" if name in ("D/ST", "DST") else name
            base_slots[pos] += count
            total_starters += count
        elif name in ("RB/WR", "WR/RB"):
            flex_slots.append({"allowed": ["RB", "WR"], "count": count})
            total_starters += count
        elif name in ("WR/TE",):
            flex_slots.append({"allowed": ["WR", "TE"], "count": count})
            total_starters += count
        elif name in ("RB/WR/TE", "W/R/T", "FLEX", "OP"):
            flex_slots.append({"allowed": ["RB", "WR", "TE"], "count": count})
            total_starters += count
        else:
            # Unknown slot type; treat as generic flex for core positions.
            flex_slots.append({"allowed": ["QB", "RB", "WR", "TE"], "count": count})
            total_starters += count

    return base_slots, flex_slots, total_starters


def load_league_owned_players(
    conn, league_id: int, my_team_name: str | None
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Load the latest roster snapshot for all players in a league from
    roster_status_history, and separate:

      - roster: players considered owned on *your* team (my_team_name).
      - owned_ids: all player_ids considered owned in this league (any team).

    Ownership is inferred from roster_status; team identity from fantasy_team.
    If my_team_name is null/empty, we treat all owned players as "yours"
    (fallback behavior).
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

    roster: List[Dict[str, Any]] = []
    owned_ids_set = set()

    my_name = (my_team_name or "").strip()

    for r in rows:
        pid = int(r["player_id"])
        status = (r["roster_status"] or "").lower()
        team = (r["fantasy_team"] or "").strip()

        is_owned = status in ("owned", "bench", "starter", "active")

        if is_owned and team:
            owned_ids_set.add(pid)

        # No my_team_name configured: treat all owned players as "yours"
        if not my_name:
            if is_owned:
                roster.append(
                    {
                        "player_id": pid,
                        "fantasy_team": team,
                        "roster_status": r["roster_status"],
                        "position": r["position"],
                    }
                )
            continue

        # With my_team_name: only include your team in roster
        if is_owned and team == my_name:
            roster.append(
                {
                    "player_id": pid,
                    "fantasy_team": team,
                    "roster_status": r["roster_status"],
                    "position": r["position"],
                }
            )

    return roster, sorted(owned_ids_set)


# ──────────────────────────────────────────────────────────────────────────────
# Recommendation logic
# ──────────────────────────────────────────────────────────────────────────────


def compute_start_sit(
    league_id: int,
    week: int,
    roster: List[Dict[str, Any]],
    rankings: Dict[int, Mapping[str, Any]],
    players_meta: Dict[int, str],
    base_slots: Dict[str, int],
    flex_slots: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Position-aware start/sit:

      - Uses base slots (QB/RB/WR/TE/K/D/ST) from league_slots.
      - Fills flex slots (RB/WR, WR/TE, FLEX, etc.) from remaining players.
      - Only considers players on YOUR roster (already filtered).
    """
    # Enrich your roster with base position + composite_score from rankings
    enriched: List[Dict[str, Any]] = []
    for r in roster:
        pid = r["player_id"]
        rank_row = rankings.get(pid)
        if not rank_row:
            continue
        base_pos = players_meta.get(pid, "").upper()
        if base_pos == "DST":
            base_pos = "D/ST"
        score = float(rank_row["composite_score"] or 0.0)
        enriched.append(
            {
                "player_id": pid,
                "base_pos": base_pos,
                "score": score,
            }
        )

    if not enriched:
        return []

    # Group by base position
    by_pos: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in enriched:
        by_pos[e["base_pos"]].append(e)

    for lst in by_pos.values():
        lst.sort(key=lambda x: -x["score"])

    starters: set[int] = set()

    # 1) Fill base position slots
    for pos, needed in base_slots.items():
        lst = by_pos.get(pos, [])
        for e in lst:
            if needed <= 0:
                break
            pid = e["player_id"]
            if pid in starters:
                continue
            starters.add(pid)
            needed -= 1

    # 2) Fill flex slots from remaining players
    remaining: Dict[int, Dict[str, Any]] = {
        e["player_id"]: e for e in enriched if e["player_id"] not in starters
    }

    for flex in flex_slots:
        allowed = flex.get("allowed", [])
        count = int(flex.get("count") or 0)
        for _ in range(count):
            candidate = None
            for e in remaining.values():
                if allowed and e["base_pos"] not in allowed:
                    continue
                if candidate is None or e["score"] > candidate["score"]:
                    candidate = e
            if not candidate:
                break
            pid = candidate["player_id"]
            starters.add(pid)
            remaining.pop(pid, None)

    # 3) Build ordered recommendations: highest scores first
    sorted_enriched = sorted(
        enriched, key=lambda x: (-x["score"], x["base_pos"], x["player_id"])
    )

    recs: List[Dict[str, Any]] = []
    for idx, e in enumerate(sorted_enriched, start=1):
        action = "start" if e["player_id"] in starters else "bench"
        recs.append(
            {
                "league_id": league_id,
                "week": week,
                "slot": f"SLOT-{idx}",
                "player_id": e["player_id"],
                "composite_score": e["score"],
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
    significantly below the average of available free agents.

    This is a simple global replacement model; can be refined by position later.
    """
    owned_set = set(owned_ids)

    free_scores: List[float] = []
    for pid, r in rankings.items():
        if pid in owned_set:
            continue
        free_scores.append(float(r["composite_score"] or 0.0))

    if not free_scores:
        return []

    avg_replacement = sum(free_scores) / len(free_scores)

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
        players_meta = load_players_meta(conn)

        for league in leagues:
            league_id = int(league["id"])
            league_name = league["league_name"]
            sport = league["sport"]
            scoring_type = league["scoring_type"]
            my_team_name = league.get("my_team_name")

            if not sport or not scoring_type:
                print(
                    f"[waiver-planner] Skipping league_id={league_id} "
                    f"({league_name}): sport/scoring_type not set"
                )
                continue

            try:
                week = get_current_week(conn, sport, scoring_type)
            except RuntimeError as e:
                print(
                    f"[waiver-planner] Skipping league_id={league_id} ({league_name}): {e}"
                )
                continue

            rankings = load_rankings_for_league_week(conn, sport, scoring_type, week)
            if not rankings:
                print(
                    f"[waiver-planner] No rankings rows for league_id={league_id} "
                    f"({league_name}), sport={sport}, scoring_type={scoring_type}, week={week}"
                )
                continue

            roster, owned_ids = load_league_owned_players(conn, league_id, my_team_name)
            if not roster:
                print(
                    f"[waiver-planner] No owned players for league_id={league_id} "
                    f"({league_name}), my_team_name={my_team_name!r}, skipping"
                )
                continue

            base_slots, flex_slots, _ = load_slot_configuration(conn, league_id)

            ss_recs = compute_start_sit(
                league_id=league_id,
                week=week,
                roster=roster,
                rankings=rankings,
                players_meta=players_meta,
                base_slots=base_slots,
                flex_slots=flex_slots,
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
