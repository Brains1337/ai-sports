#!/usr/bin/env python3
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
SEASON = int(os.getenv("ESPN_SEASON", "2026"))

STARTER_WEIGHTS = {
    "QB": 1.8,
    "RB": 2.4,
    "WR": 2.2,
    "TE": 1.5,
    "K": 0.4,
    "D/ST": 0.6,
    "DST": 0.6,
}


def now():
    return datetime.now(timezone.utc)


def load_leagues(conn):
    return conn.execute(text("""
        select id, external_league_id, league_name, payload
        from leagues
        where season = :season
        order by id
    """), {"season": SEASON}).mappings().all()


def load_slot_counts(conn, league_id):
    rows = conn.execute(text("""
        select slot_name, slot_count
        from league_slots
        where league_id = :league_id
    """), {"league_id": league_id}).mappings().all()
    return {row["slot_name"]: int(row["slot_count"]) for row in rows}


def load_player_pool(conn):
    rows = conn.execute(text("""
        with latest_proj as (
            select distinct on (player_id, source_name)
                player_id,
                source_name,
                projected_points,
                adp,
                receptions,
                pass_yd,
                rush_yd,
                rec_yd,
                injury_status,
                news_summary,
                payload,
                fetched_at
            from projections
            where source_name in ('fantasypros_proj', 'fantasypros_ecr')
            order by player_id, source_name, fetched_at desc
        )
        select
            p.id as player_id,
            p.external_player_id,
            p.player_name,
            p.pos,
            p.pro_team_id,
            p.bye_week,
            p.eligible_slot_names,
            coalesce(p.percent_owned, 0) as percent_owned,
            coalesce(
              proj.projected_points,
              nullif(proj.payload->>'projected_points', '')::numeric,
              nullif(proj.payload->>'fantasy_points', '')::numeric,
              nullif(proj.payload->>'fpts', '')::numeric,
              nullif(proj.payload->>'points', '')::numeric,
              nullif(proj.payload#>>'{stats,points}', '')::numeric,
              nullif(proj.payload#>>'{stats,points_ppr}', '')::numeric
            ) as projected_points,
            coalesce(
              proj.receptions,
              nullif(proj.payload->>'receptions', '')::numeric,
              nullif(proj.payload#>>'{stats,receptions}', '')::numeric,
              nullif(proj.payload#>>'{stats,rec_att}', '')::numeric
            ) as receptions,
            coalesce(
              proj.pass_yd,
              nullif(proj.payload->>'pass_yd', '')::numeric,
              nullif(proj.payload#>>'{stats,pass_yds}', '')::numeric
            ) as pass_yd,
            coalesce(
              proj.rush_yd,
              nullif(proj.payload->>'rush_yd', '')::numeric,
              nullif(proj.payload#>>'{stats,rush_yds}', '')::numeric
            ) as rush_yd,
            coalesce(
              proj.rec_yd,
              nullif(proj.payload->>'rec_yd', '')::numeric,
              nullif(proj.payload#>>'{stats,rec_yds}', '')::numeric
            ) as rec_yd,
            proj.injury_status as proj_injury_status,
            ecr.adp,
            ecr.news_summary as ecr_summary
        from players p
        left join latest_proj proj
          on proj.player_id = p.id and proj.source_name = 'fantasypros_proj'
        left join latest_proj ecr
          on ecr.player_id = p.id and ecr.source_name = 'fantasypros_ecr'
        where p.platform = 'espn'
    """)).mappings().all()
    return [dict(r) for r in rows]

def parse_ecr_rank(summary):
    if not summary:
        return None
    marker = "ECR="
    if marker not in summary:
        return None
    try:
        part = summary.split(marker, 1)[1].split(",", 1)[0].strip()
        return float(part)
    except Exception:
        return None


def derive_slot_priority(slot_counts):
    priority = defaultdict(float)
    priority["QB"] += slot_counts.get("QB", 0) * STARTER_WEIGHTS["QB"]
    priority["RB"] += slot_counts.get("RB", 0) * STARTER_WEIGHTS["RB"]
    priority["WR"] += slot_counts.get("WR", 0) * STARTER_WEIGHTS["WR"]
    priority["TE"] += slot_counts.get("TE", 0) * STARTER_WEIGHTS["TE"]
    priority["K"] += slot_counts.get("K", 0) * STARTER_WEIGHTS["K"]
    priority["D/ST"] += slot_counts.get("D/ST", 0) * STARTER_WEIGHTS["D/ST"]
    priority["QB"] += slot_counts.get("OP", 0) * 0.9
    priority["RB"] += slot_counts.get("RB/WR", 0) * 0.8 + slot_counts.get("Flex", 0) * 0.8
    priority["WR"] += slot_counts.get("RB/WR", 0) * 0.8 + slot_counts.get("WR/TE", 0) * 0.8 + slot_counts.get("Flex", 0) * 0.8
    priority["TE"] += slot_counts.get("WR/TE", 0) * 0.7 + slot_counts.get("Flex", 0) * 0.4
    return priority


def score_player(player, slot_priority, league_name):
    pos = player.get("pos") or "UNKNOWN"
    projected_points = float(player.get("projected_points") or 0)
    percent_owned = float(player.get("percent_owned") or 0)
    adp = player.get("adp")
    bye_week = player.get("bye_week")
    ecr_rank = parse_ecr_rank(player.get("ecr_summary"))

    scarcity_bonus = slot_priority.get(pos, 0) * 2.0
    ownership_bonus = min(percent_owned, 100.0) * 0.03
    adp_bonus = 0.0
    if adp not in (None, ""):
        try:
            adp_bonus = max(0.0, 12.0 - (float(adp) / 20.0))
        except Exception:
            adp_bonus = 0.0

    ecr_bonus = 0.0
    if ecr_rank:
        ecr_bonus = max(0.0, 15.0 - (ecr_rank / 15.0))

    bye_penalty = 0.0
    if bye_week in (13, 14):
        bye_penalty = 1.5
    elif bye_week in (11, 12):
        bye_penalty = 0.7

    injury_penalty = 0.0
    if player.get("proj_injury_status"):
        injury_penalty = 2.0

    guillotine_bonus = 0.0
    name = (league_name or "").lower()
    if "guillotine" in name or "dark" in name:
        if pos in ("RB", "WR", "TE", "QB"):
            guillotine_bonus += 1.5
        if projected_points >= 220:
            guillotine_bonus += 1.0

    score = projected_points + scarcity_bonus + ownership_bonus + adp_bonus + ecr_bonus + guillotine_bonus - bye_penalty - injury_penalty
    notes = {
        "projected_points": round(projected_points, 2),
        "scarcity_bonus": round(scarcity_bonus, 2),
        "ownership_bonus": round(ownership_bonus, 2),
        "adp_bonus": round(adp_bonus, 2),
        "ecr_bonus": round(ecr_bonus, 2),
        "guillotine_bonus": round(guillotine_bonus, 2),
        "bye_penalty": round(bye_penalty, 2),
        "injury_penalty": round(injury_penalty, 2),
        "bye_week": bye_week,
        "percent_owned": round(percent_owned, 2),
    }
    return round(score, 2), notes


def build_for_league(conn, league, player_pool):
    league_id = league["id"]
    league_name = league["league_name"]
    slot_counts = load_slot_counts(conn, league_id)
    slot_priority = derive_slot_priority(slot_counts)
    batch_created_at = now()

    scored = []
    for player in player_pool:
        if player.get("projected_points") in (None, ""):
            continue
        score, notes = score_player(player, slot_priority, league_name)
        scored.append({
            "player_id": player["player_id"],
            "player_name": player["player_name"],
            "pos": player["pos"],
            "score": score,
            "notes": notes,
        })

    scored.sort(key=lambda r: (-r["score"], r["player_name"]))

    conn.execute(
        text("delete from derived_rankings where league_id = :league_id and source_name = 'v1_model'"),
        {"league_id": league_id},
    )

    for idx, row in enumerate(scored, start=1):
        conn.execute(text("""
            insert into derived_rankings (
              league_id, player_id, source_name, adjusted_rank, adjusted_score, score_delta, notes, created_at
            ) values (
              :league_id, :player_id, 'v1_model', :adjusted_rank, :adjusted_score, null, :notes, :created_at
            )
        """), {
            "league_id": league_id,
            "player_id": row["player_id"],
            "adjusted_rank": idx,
            "adjusted_score": row["score"],
            "notes": json.dumps(row["notes"]),
            "created_at": batch_created_at,
        })

    return {
        "league_id": league_id,
        "league_name": league_name,
        "players_ranked": len(scored),
        "slot_priority": dict(slot_priority),
    }

def main():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as conn:
        leagues = load_leagues(conn)
        pool = load_player_pool(conn)
        results = [build_for_league(conn, league, pool) for league in leagues]
    print(json.dumps({"season": SEASON, "results": results}, indent=2))


if __name__ == "__main__":
    main()
