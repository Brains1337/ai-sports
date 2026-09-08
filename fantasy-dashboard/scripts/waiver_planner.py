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
  - `leagues` has: sport, scoring_type, platform, waiver_type, my_team_name.
  - `players` has: id, name, pos, team, sport.

Platforms supported:
  NFL  : espn-nfl, yahoo-nfl, fantrax-nfl
  NCAAF: yahoo-cfb, fantrax-cfb
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]

# Minimum improvement (in composite_score) to consider a waiver target
MIN_WAIVER_DELTA = float(os.getenv("MIN_WAIVER_DELTA", "2.0"))
# How many top waiver targets to keep per league
MAX_WAIVER_TARGETS = int(os.getenv("MAX_WAIVER_TARGETS", "10"))
# Minimum gap below position-peer replacement to flag a drop candidate
MIN_DROP_DELTA = float(os.getenv("MIN_DROP_DELTA", "1.5"))
# Telegram bot token + chat id (optional — alerts skipped if not set)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Rankings source_name prefixes to use per platform family.
# These must match the source_name values written by build_rankings.py /
# sync_cfbd_cfb_projections.py etc.
_PLATFORM_SOURCE_PREFIX: Dict[str, str] = {
    "espn-nfl":    "espn_nfl_",
    "yahoo-nfl":   "yahoo_nfl_",
    "fantrax-nfl": "fantrax_nfl_",
    "yahoo-cfb":   "cfbd_cfb_proj_yahoo",
    "fantrax-cfb": "cfbd_cfb_proj_fantrax",
}

# Waiver type labels (cosmetic for alerts)
_WAIVER_LABELS: Dict[str, str] = {
    "faab":     "FAAB",
    "priority": "Priority",
    "waivers":  "Waivers",
    "free":     "Free Agent",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# Telegram helper
# ──────────────────────────────────────────────────────────────────────────────


def send_telegram(message: str) -> None:
    """Fire a Telegram alert if credentials are configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.request  # stdlib — no extra dep
        import urllib.parse

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        ).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"[waiver-planner] Telegram send failed: {exc}", file=sys.stderr)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers to load core entities
# ──────────────────────────────────────────────────────────────────────────────


def load_active_leagues(conn) -> Sequence[Mapping[str, Any]]:
    """
    Load leagues we want recommendations for.
    Skips non-roster formats (ESPN pick'em).
    """
    rows = conn.execute(text("""
        select id,
               league_name,
               sport,
               scoring_type,
               platform,
               waiver_type,
               season,
               my_team_name
        from   leagues
        where  platform not in ('espn-pickem')
        order  by id
    """)).mappings().all()
    return rows


def get_current_week(conn, sport: str, scoring_type: str) -> int:
    """
    Determine current week by taking max(week) from rankings for the given
    sport/scoring_type. Override with FANTASY_WEEK env var if set.
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
        from   rankings
        where  sport        = :sport
          and  scoring_type = :scoring_type
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
    conn,
    sport: str,
    scoring_type: str,
    week: int,
    platform: str = "",
) -> Dict[int, Mapping[str, Any]]:
    """
    Load rankings rows for this sport/scoring_type/week, keyed by player_id.
    When `platform` is provided and maps to a known source_name prefix, the
    query is further filtered so NFL and NCAAF projections don't bleed across
    leagues.
    """
    source_prefix = _PLATFORM_SOURCE_PREFIX.get(platform, "")

    if source_prefix:
        rows = (
            conn.execute(
                text("""
            select player_id,
                   proj_pts,
                   opp_team,
                   def_strength,
                   composite_score
            from   rankings
            where  sport        = :sport
              and  scoring_type = :scoring_type
              and  week         = :week
              and  source_name  like :src_prefix
            """),
                {
                    "sport": sport,
                    "scoring_type": scoring_type,
                    "week": week,
                    "src_prefix": source_prefix + "%",
                },
            )
            .mappings()
            .all()
        )
    else:
        rows = (
            conn.execute(
                text("""
            select player_id,
                   proj_pts,
                   opp_team,
                   def_strength,
                   composite_score
            from   rankings
            where  sport        = :sport
              and  scoring_type = :scoring_type
              and  week         = :week
            """),
                {"sport": sport, "scoring_type": scoring_type, "week": week},
            )
            .mappings()
            .all()
        )

    return {int(r["player_id"]): r for r in rows}


def load_players_meta(conn) -> Dict[int, Dict[str, Any]]:
    """
    Load name, position and NFL/NCAAF team per player.
    Returns dict keyed by player_id with keys: name, pos, team.
    """
    rows = conn.execute(text("""
        select id, name, pos, team
        from   players
    """)).mappings().all()
    meta: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        pid = int(r["id"])
        pos = (r["pos"] or "").upper()
        if pos == "DST":
            pos = "D/ST"
        meta[pid] = {
            "name": r["name"] or f"Player#{pid}",
            "pos":  pos,
            "team": r["team"] or "",
        }
    return meta


def load_slot_configuration(
    conn, league_id: int
) -> Tuple[Dict[str, int], List[Dict[str, Any]], int]:
    """
    From league_slots, derive:
      - base_slots : required starters per base position (QB/RB/WR/TE/K/D/ST)
      - flex_slots : list of flex slot specs {allowed: [...], count: int}
      - total_starters: total non-bench slot count
    """
    rows = (
        conn.execute(
            text("""
        select slot_name, slot_count
        from   league_slots
        where  league_id = :league_id
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
        name  = (r["slot_name"] or "").upper()
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
            flex_slots.append({"allowed": ["QB", "RB", "WR", "TE"], "count": count})
            total_starters += count

    return dict(base_slots), flex_slots, total_starters


def load_league_owned_players(
    conn, league_id: int, my_team_name: Optional[str]
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Load latest roster snapshot for all players in a league, return:
      - roster    : players owned by MY team (my_team_name), or all owned
                    players if my_team_name is unset.
      - owned_ids : every player_id owned by ANY team in this league.
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
          from   roster_status_history
          where  league_id = :league_id
          order  by player_id, fetched_at desc
        )
        select player_id,
               fantasy_team,
               roster_status,
               position
        from   latest
        """),
            {"league_id": league_id},
        )
        .mappings()
        .all()
    )

    roster: List[Dict[str, Any]] = []
    owned_ids_set: set = set()
    my_name = (my_team_name or "").strip()

    for r in rows:
        pid    = int(r["player_id"])
        status = (r["roster_status"] or "").lower()
        team   = (r["fantasy_team"]  or "").strip()

        is_owned = status in ("owned", "bench", "starter", "active")

        if is_owned and team:
            owned_ids_set.add(pid)

        if not my_name:
            if is_owned:
                roster.append(
                    {
                        "player_id":     pid,
                        "fantasy_team":  team,
                        "roster_status": r["roster_status"],
                        "position":      r["position"],
                    }
                )
            continue

        if is_owned and team == my_name:
            roster.append(
                {
                    "player_id":     pid,
                    "fantasy_team":  team,
                    "roster_status": r["roster_status"],
                    "position":      r["position"],
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
    players_meta: Dict[int, Dict[str, Any]],
    base_slots: Dict[str, int],
    flex_slots: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Position-aware start/sit.
    Fills base slots first, then flex slots from remaining players.
    """
    enriched: List[Dict[str, Any]] = []
    for r in roster:
        pid = r["player_id"]
        rank_row = rankings.get(pid)
        if not rank_row:
            continue
        meta     = players_meta.get(pid, {})
        base_pos = meta.get("pos", "").upper()
        if base_pos == "DST":
            base_pos = "D/ST"
        score = float(rank_row["composite_score"] or 0.0)
        enriched.append(
            {
                "player_id": pid,
                "base_pos":  base_pos,
                "score":     score,
                "name":      meta.get("name", ""),
                "opp_team":  rank_row.get("opp_team") or "",
            }
        )

    if not enriched:
        return []

    by_pos: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in enriched:
        by_pos[e["base_pos"]].append(e)
    for lst in by_pos.values():
        lst.sort(key=lambda x: -x["score"])

    starters: set = set()

    # 1) Base position slots
    for pos, needed in base_slots.items():
        for e in by_pos.get(pos, []):
            if needed <= 0:
                break
            pid = e["player_id"]
            if pid in starters:
                continue
            starters.add(pid)
            needed -= 1

    # 2) Flex slots
    remaining: Dict[int, Dict[str, Any]] = {
        e["player_id"]: e for e in enriched if e["player_id"] not in starters
    }
    for flex in flex_slots:
        allowed = flex.get("allowed", [])
        count   = int(flex.get("count") or 0)
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

    sorted_enriched = sorted(
        enriched, key=lambda x: (-x["score"], x["base_pos"], x["player_id"])
    )

    recs: List[Dict[str, Any]] = []
    for idx, e in enumerate(sorted_enriched, start=1):
        action = "start" if e["player_id"] in starters else "bench"
        opp    = e["opp_team"]
        rationale = (
            f"proj score {e['score']:.1f}; opp {opp}" if opp
            else f"proj score {e['score']:.1f}"
        )
        recs.append(
            {
                "league_id":          league_id,
                "week":               week,
                "slot":               f"SLOT-{idx}",
                "player_id":          e["player_id"],
                "composite_score":    e["score"],
                "recommended_action": action,
                "rationale":          rationale,
            }
        )
    return recs


def compute_waiver_targets(
    league_id: int,
    week: int,
    rankings: Dict[int, Mapping[str, Any]],
    owned_ids: List[int],
    start_sit_recs: List[Dict[str, Any]],
    players_meta: Dict[int, Dict[str, Any]],
    waiver_type: str = "",
) -> List[Dict[str, Any]]:
    """
    Identify free-agent waiver targets whose composite_score significantly
    exceeds your worst current starter. Includes player name + waiver type
    in the rationale string for Telegram alerts.
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
    wt_label = _WAIVER_LABELS.get((waiver_type or "").lower(), "Waivers")

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
        meta  = players_meta.get(pid, {})
        name  = meta.get("name", f"Player#{pid}")
        pos   = meta.get("pos", "")
        team  = meta.get("team", "")
        proj  = float(rankings[pid]["proj_pts"] or 0.0)
        delta = score - worst_starter_score
        opp   = rankings[pid].get("opp_team") or ""
        rationale = (
            f"{wt_label}: {name} ({pos}, {team}) proj {proj:.1f}pts, "
            f"score {score:.1f} (+{delta:.1f} vs worst starter)"
            + (f", vs {opp}" if opp else "")
        )
        waiver_recs.append(
            {
                "league_id":                   league_id,
                "week":                        week,
                "player_id":                   pid,
                "projected_pts":               proj,
                "priority_score":              delta,
                "recommended_drop_player_id":  None,
                "status":                      "open",
                "rationale":                   rationale,
            }
        )
    return waiver_recs


def compute_drop_candidates(
    league_id: int,
    week: int,
    roster: List[Dict[str, Any]],
    rankings: Dict[int, Mapping[str, Any]],
    owned_ids: List[int],
    players_meta: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Position-aware drop candidates: compare each rostered player's
    composite_score against the average free-agent score AT THE SAME POSITION.

    Falls back to global average when no free agents exist for that position.
    """
    owned_set = set(owned_ids)

    # Build free-agent score pools by position
    fa_scores_by_pos: Dict[str, List[float]] = defaultdict(list)
    fa_scores_global: List[float] = []
    for pid, r in rankings.items():
        if pid in owned_set:
            continue
        score = float(r["composite_score"] or 0.0)
        pos   = players_meta.get(pid, {}).get("pos", "").upper()
        fa_scores_by_pos[pos].append(score)
        fa_scores_global.append(score)

    if not fa_scores_global:
        return []

    avg_global = sum(fa_scores_global) / len(fa_scores_global)

    drop_recs: List[Dict[str, Any]] = []
    for r in roster:
        pid      = r["player_id"]
        rank_row = rankings.get(pid)
        if not rank_row:
            continue
        score = float(rank_row["composite_score"] or 0.0)
        pos   = players_meta.get(pid, {}).get("pos", "").upper()
        name  = players_meta.get(pid, {}).get("name", f"Player#{pid}")

        pos_pool = fa_scores_by_pos.get(pos, [])
        avg_repl = (sum(pos_pool) / len(pos_pool)) if pos_pool else avg_global

        delta = avg_repl - score
        if delta >= MIN_DROP_DELTA:
            rationale = (
                f"{name} ({pos}) score {score:.1f} vs avg FA@pos {avg_repl:.1f} "
                f"(delta -{delta:.1f})"
            )
            drop_recs.append(
                {
                    "league_id":         league_id,
                    "week":              week,
                    "player_id":         pid,
                    "composite_score":   score,
                    "replacement_delta": delta,
                    "reason_code":       "below_replacement",
                    "rationale":         rationale,
                }
            )

    return drop_recs


# ──────────────────────────────────────────────────────────────────────────────
# Telegram alert builders
# ──────────────────────────────────────────────────────────────────────────────


def build_waiver_alert(
    league_name: str,
    week: int,
    waiver_recs: List[Dict[str, Any]],
    drop_recs: List[Dict[str, Any]],
    players_meta: Dict[int, Dict[str, Any]],
    waiver_type: str,
) -> Optional[str]:
    """Build a Telegram message for top waiver targets (up to 3)."""
    if not waiver_recs:
        return None

    wt_label = _WAIVER_LABELS.get((waiver_type or "").lower(), "Waivers")
    lines = [
        f"📈 <b>WAIVER ALERT — {league_name} Week {week}</b>",
        f"Type: {wt_label}\n",
    ]

    drop_names = [
        players_meta.get(d["player_id"], {}).get("name", f"Player#{d['player_id']}")
        for d in drop_recs[:3]
    ]

    for i, rec in enumerate(waiver_recs[:3], start=1):
        pid   = rec["player_id"]
        meta  = players_meta.get(pid, {})
        name  = meta.get("name", f"Player#{pid}")
        pos   = meta.get("pos", "")
        team  = meta.get("team", "")
        proj  = rec["projected_pts"]
        score = rec["priority_score"]
        drop  = drop_names[i - 1] if i - 1 < len(drop_names) else "—"
        lines.append(
            f"{i}. <b>{name}</b> ({pos}, {team}) "
            f"proj {proj:.1f}pts | +{score:.1f} edge"
            f" | Drop: {drop}"
        )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ──────────────────────────────────────────────────────────────────────────────


def persist_start_sit(
    conn, league_id: int, week: int, recs: List[Dict[str, Any]]
) -> None:
    conn.execute(
        text("""
        delete from start_sit_recommendations
        where  league_id = :league_id
          and  week      = :week
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
        where  league_id = :league_id
          and  week      = :week
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
        where  league_id = :league_id
          and  week      = :week
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
    total_written = 0

    with engine.begin() as conn:
        leagues      = load_active_leagues(conn)
        players_meta = load_players_meta(conn)

        for league in leagues:
            league_id    = int(league["id"])
            league_name  = league["league_name"]
            sport        = league["sport"]
            scoring_type = league["scoring_type"]
            platform     = league.get("platform") or ""
            waiver_type  = league.get("waiver_type") or ""
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

            rankings = load_rankings_for_league_week(
                conn, sport, scoring_type, week, platform=platform
            )
            if not rankings:
                print(
                    f"[waiver-planner] No rankings rows for league_id={league_id} "
                    f"({league_name}), platform={platform}, "
                    f"sport={sport}, scoring_type={scoring_type}, week={week}"
                )
                continue

            roster, owned_ids = load_league_owned_players(
                conn, league_id, my_team_name
            )
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
                players_meta=players_meta,
                waiver_type=waiver_type,
            )
            dc_recs = compute_drop_candidates(
                league_id=league_id,
                week=week,
                roster=roster,
                rankings=rankings,
                owned_ids=owned_ids,
                players_meta=players_meta,
            )

            persist_start_sit(conn, league_id, week, ss_recs)
            persist_waiver_targets(conn, league_id, week, wt_recs)
            persist_drop_candidates(conn, league_id, week, dc_recs)

            league_written = len(ss_recs) + len(wt_recs) + len(dc_recs)
            total_written += league_written

            if league_written == 0:
                print(
                    f"[waiver-planner] WARNING: league_id={league_id} ({league_name}) "
                    f"produced 0 rows — check rankings xref and roster_status_history",
                    file=sys.stderr,
                )
            else:
                alert = build_waiver_alert(
                    league_name=league_name,
                    week=week,
                    waiver_recs=wt_recs,
                    drop_recs=dc_recs,
                    players_meta=players_meta,
                    waiver_type=waiver_type,
                )
                if alert:
                    send_telegram(alert)

            summary.append(
                {
                    "league_id":             league_id,
                    "league_name":           league_name,
                    "sport":                 sport,
                    "scoring_type":          scoring_type,
                    "platform":              platform,
                    "week":                  week,
                    "roster_size":           len(roster),
                    "start_sit_count":       len(ss_recs),
                    "waiver_targets_count":  len(wt_recs),
                    "drop_candidates_count": len(dc_recs),
                }
            )

    print(json.dumps({"generated_at": now().isoformat(), "leagues": summary}, indent=2))

    if total_written == 0 and summary:
        print(
            "[waiver-planner] CRITICAL: 0 rows written across all leagues — "
            "check player xref, rankings population, and roster_status_history",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
