#!/usr/bin/env python3
import hashlib
import json
import os
from datetime import datetime, timezone

import requests
from sqlalchemy import create_engine, text

SEASON = int(os.getenv("ESPN_SEASON", "2026"))
LEAGUE_IDS = [
    int(value.strip())
    for value in os.getenv(
        "ESPN_LEAGUE_IDS",
        "719429857,209442251",
    ).split(",")
    if value.strip()
]

DATABASE_URL = os.environ["DATABASE_URL"]

BASE = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
COOKIES = {
    key: value
    for key, value in {
        "espn_s2": os.getenv("ESPN_S2"),
        "SWID": os.getenv("ESPN_SWID"),
    }.items()
    if value
}
HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "User-Agent": "fantasy-dashboard/0.1",
}

POSITION_MAP = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "D/ST",
}

SLOT_MAP = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE",
    6: "TE", 7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL",
    12: "CB", 13: "S", 14: "DB", 15: "DP", 16: "D/ST", 17: "K",
    18: "P", 19: "HC", 20: "Bench", 21: "IR", 22: "Flex",
    23: "Flex", 24: "Taxi", 25: "Rookie",
}

def now():
    return datetime.now(timezone.utc)

def content_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

def request_json(url, params=None, headers=None):
    response = requests.get(
        url,
        params=params,
        headers={**HEADERS, **(headers or {})},
        cookies=COOKIES,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()

def record_source(conn, source_name, source_key, payload):
    conn.execute(
        text("""
            insert into sources
              (source_name, source_key, fetched_at, content_hash, status, meta)
            values
              (:source_name, :source_key, :fetched_at, :content_hash, 'ok', cast(:meta as jsonb))
        """),
        {
            "source_name": source_name,
            "source_key": source_key,
            "fetched_at": now(),
            "content_hash": content_hash(payload),
            "meta": json.dumps({"record_count": len(payload) if isinstance(payload, list) else 1}),
        },
    )

def sync_leagues(conn):
    for league_id in LEAGUE_IDS:
        url = f"{BASE}/segments/0/leagues/{league_id}"
        payload = request_json(url, params=[("view", "mSettings")])

        settings = payload.get("settings", {})
        status = payload.get("status", {})
        league_name = settings.get("name") or payload.get("name") or f"ESPN {league_id}"

        result = conn.execute(
            text("""
                insert into leagues (
                  external_league_id, platform, season, league_name, scoring_type,
                  player_rank_type, scoring_enhancement_type, team_count, teams_joined,
                  draft_type, time_per_selection, faab_budget, waiver_type, payload, updated_at
                ) values (
                  :external_league_id, 'espn', :season, :league_name, :scoring_type,
                  :player_rank_type, :scoring_enhancement_type, :team_count, :teams_joined,
                  :draft_type, :time_per_selection, :faab_budget, :waiver_type,
                  cast(:payload as jsonb), :updated_at
                )
                on conflict (external_league_id) do update set
                  season = excluded.season,
                  league_name = excluded.league_name,
                  scoring_type = excluded.scoring_type,
                  player_rank_type = excluded.player_rank_type,
                  scoring_enhancement_type = excluded.scoring_enhancement_type,
                  team_count = excluded.team_count,
                  teams_joined = excluded.teams_joined,
                  draft_type = excluded.draft_type,
                  time_per_selection = excluded.time_per_selection,
                  faab_budget = excluded.faab_budget,
                  waiver_type = excluded.waiver_type,
                  payload = excluded.payload,
                  updated_at = excluded.updated_at
                returning id
            """),
            {
                "external_league_id": league_id,
                "season": SEASON,
                "league_name": league_name,
                "scoring_type": settings.get("scoringType"),
                "player_rank_type": settings.get("playerRankType"),
                "scoring_enhancement_type": settings.get("scoringSettings", {}).get("scoringType"),
                "team_count": settings.get("size"),
                "teams_joined": status.get("numberOfTeams"),
                "draft_type": settings.get("draftSettings", {}).get("type"),
                "time_per_selection": settings.get("draftSettings", {}).get("timePerSelection"),
                "faab_budget": settings.get("acquisitionSettings", {}).get("acquisitionBudget"),
                "waiver_type": settings.get("acquisitionSettings", {}).get("waiverType"),
                "payload": json.dumps(payload),
                "updated_at": now(),
            },
        )
        league_db_id = result.scalar_one()

        conn.execute(text("delete from league_slots where league_id = :league_id"), {"league_id": league_db_id})
        lineup_counts = settings.get("rosterSettings", {}).get("lineupSlotCounts", {})
        for slot_id, slot_count in lineup_counts.items():
            try:
                slot_id_int = int(slot_id)
                count_int = int(slot_count)
            except (ValueError, TypeError):
                continue
            if count_int:
                conn.execute(
                    text("""
                        insert into league_slots (league_id, slot_name, slot_count)
                        values (:league_id, :slot_name, :slot_count)
                    """),
                    {
                        "league_id": league_db_id,
                        "slot_name": SLOT_MAP.get(slot_id_int, f"slot_{slot_id_int}"),
                        "slot_count": count_int,
                    },
                )

        record_source(conn, "espn", f"league:{league_id}:mSettings:{SEASON}", payload)
        print(f"Synced league {league_id}: {league_name}")

def sync_pro_teams(conn):
    payload = request_json(BASE, params=[("view", "proTeamSchedules_wl")])
    teams = payload.get("settings", {}).get("proTeams", [])

    for team in teams:
        external_team_id = team.get("id")
        if external_team_id is None:
            continue
        conn.execute(
            text("""
                insert into pro_teams (
                  platform, external_team_id, season, team_name, team_abbrev, bye_week, payload
                ) values (
                  'espn', :external_team_id, :season, :team_name, :team_abbrev, :bye_week,
                  cast(:payload as jsonb)
                )
                on conflict (platform, external_team_id, season) do update set
                  team_name = excluded.team_name,
                  team_abbrev = excluded.team_abbrev,
                  bye_week = excluded.bye_week,
                  payload = excluded.payload
            """),
            {
                "external_team_id": external_team_id,
                "season": SEASON,
                "team_name": team.get("name"),
                "team_abbrev": team.get("abbrev"),
                "bye_week": team.get("byeWeek"),
                "payload": json.dumps(team),
            },
        )

    record_source(conn, "espn", f"proTeamSchedules_wl:{SEASON}", payload)
    print(f"Synced {len(teams)} pro teams")

def sync_players(conn):
    fantasy_filter = {
        "players": {
            "limit": 2000,
            "filterActive": {"value": True},
        }
    }
    payload = request_json(
        f"{BASE}/players",
        params=[("view", "players_wl")],
        headers={"X-Fantasy-Filter": json.dumps(fantasy_filter, separators=(",", ":"))},
    )

    items = payload.get("players", payload) if isinstance(payload, dict) else payload
    count = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        player = item.get("player", item)
        player_id = player.get("id")
        player_name = player.get("fullName") or player.get("name")
        if player_id is None or not player_name:
            continue

        ownership = item.get("ownership") or player.get("ownership") or {}
        eligible = player.get("eligibleSlots", [])
        eligible_names = "|".join(
            SLOT_MAP.get(int(slot), f"slot_{slot}") for slot in eligible if str(slot).isdigit()
        )
        pro_team_id = player.get("proTeamId") or 0
        bye_week = conn.execute(
            text("""
                select bye_week
                from pro_teams
                where platform = 'espn'
                  and external_team_id = :team_id
                  and season = :season
            """),
            {"team_id": pro_team_id, "season": SEASON},
        ).scalar()

        conn.execute(
            text("""
                insert into players (
                  platform, external_player_id, player_name, first_name, last_name, pos,
                  default_position_id, pro_team_id, bye_week, eligible_slot_names,
                  percent_owned, payload
                ) values (
                  'espn', :external_player_id, :player_name, :first_name, :last_name, :pos,
                  :default_position_id, :pro_team_id, :bye_week, :eligible_slot_names,
                  :percent_owned, cast(:payload as jsonb)
                )
                on conflict (platform, external_player_id) do update set
                  player_name = excluded.player_name,
                  first_name = excluded.first_name,
                  last_name = excluded.last_name,
                  pos = excluded.pos,
                  default_position_id = excluded.default_position_id,
                  pro_team_id = excluded.pro_team_id,
                  bye_week = excluded.bye_week,
                  eligible_slot_names = excluded.eligible_slot_names,
                  percent_owned = excluded.percent_owned,
                  payload = excluded.payload
            """),
            {
                "external_player_id": player_id,
                "player_name": player_name,
                "first_name": player.get("firstName"),
                "last_name": player.get("lastName"),
                "pos": POSITION_MAP.get(player.get("defaultPositionId"), f"POS_{player.get('defaultPositionId')}"),
                "default_position_id": player.get("defaultPositionId"),
                "pro_team_id": pro_team_id,
                "bye_week": bye_week,
                "eligible_slot_names": eligible_names,
                "percent_owned": ownership.get("percentOwned"),
                "payload": json.dumps(player),
            },
        )
        count += 1

    record_source(conn, "espn", f"players_wl:{SEASON}", payload)
    print(f"Synced {count} players")

def main():
    if not COOKIES:
        raise SystemExit("Missing ESPN_S2 and/or ESPN_SWID in environment")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as conn:
        sync_leagues(conn)
        sync_pro_teams(conn)
        sync_players(conn)

if __name__ == "__main__":
    main()
