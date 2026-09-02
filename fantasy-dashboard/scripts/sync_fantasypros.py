#!/usr/bin/env python3
import hashlib
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from sqlalchemy import create_engine, text
from urllib3.util.retry import Retry

SEASON = int(os.getenv("FP_SEASON", os.getenv("ESPN_SEASON", "2026")))
DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.getenv("FANTASYPROS_API_KEY", "").strip()
BASE_URL = "https://api.fantasypros.com/public/v2/json"
CACHE_ROOT = Path("/data/cache/fantasypros") / str(SEASON)

HEADERS = {
    "x-api-key": API_KEY,
    "Accept": "application/json",
    "User-Agent": "fantasy-dashboard/0.1",
}

RANKING_POSITIONS = ["qb", "rb", "wr", "te", "k", "dst"]
PROJECTION_POSITIONS = ["qb", "rb", "wr", "te", "k", "dst"]

REQUEST_TIMEOUT = (
    int(os.getenv("FP_CONNECT_TIMEOUT", "10")),
    int(os.getenv("FP_READ_TIMEOUT", "90")),
)
MAX_ATTEMPTS = int(os.getenv("FP_MAX_ATTEMPTS", "6"))
REQUEST_DELAY_SECONDS = float(os.getenv("FP_REQUEST_DELAY_SECONDS", "3"))
USE_CACHE = os.getenv("FP_USE_CACHE", "true").lower() not in {"0", "false", "no"}
REFRESH_CACHE = os.getenv("FP_REFRESH_CACHE", "false").lower() in {"1", "true", "yes"}

TEAM_MAP = {
    "ARI": 22,
    "ATL": 1,
    "BAL": 33,
    "BUF": 2,
    "CAR": 29,
    "CHI": 3,
    "CIN": 4,
    "CLE": 5,
    "DAL": 6,
    "DEN": 7,
    "DET": 8,
    "GB": 9,
    "HOU": 34,
    "IND": 11,
    "JAX": 30,
    "KC": 12,
    "LV": 13,
    "LAC": 24,
    "LAR": 14,
    "MIA": 15,
    "MIN": 16,
    "NE": 17,
    "NO": 18,
    "NYG": 19,
    "NYJ": 20,
    "PHI": 21,
    "PIT": 23,
    "SEA": 26,
    "SF": 25,
    "TB": 27,
    "TEN": 10,
    "WAS": 28,
}

SESSION = requests.Session()

retry = Retry(
    total=3,
    connect=3,
    read=3,
    status=3,
    backoff_factor=1,
    status_forcelist=[408, 429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET"]),
    respect_retry_after_header=True,
    raise_on_status=False,
)

adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)


def utcnow():
    return datetime.now(timezone.utc)


def sha256_json(data):
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def ensure_cache_dir():
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)


def cache_path(name):
    return CACHE_ROOT / name


def cache_write(name, data):
    ensure_cache_dir()
    path = cache_path(name)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def cache_read(name):
    path = cache_path(name)
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Ignoring unreadable cache file {path}: {exc}")
        return None


def sleep_with_jitter(seconds):
    time.sleep(max(0, seconds) + random.uniform(0, 1))


def retry_delay(response, attempt):
    retry_after = response.headers.get("Retry-After")

    if retry_after:
        try:
            return min(float(retry_after), 300)
        except ValueError:
            pass

    return min(60, 2 ** attempt)


def get_json(path, params=None):
    url = f"{BASE_URL}{path}"
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = SESSION.get(
                url,
                headers=HEADERS,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 429:
                if attempt == MAX_ATTEMPTS:
                    response.raise_for_status()

                delay = retry_delay(response, attempt)
                print(
                    f"Rate limited: {path} params={params}; "
                    f"waiting about {delay:.0f}s before retry "
                    f"({attempt}/{MAX_ATTEMPTS})"
                )
                sleep_with_jitter(delay)
                continue

            if response.status_code in {408, 500, 502, 503, 504}:
                if attempt == MAX_ATTEMPTS:
                    response.raise_for_status()

                delay = retry_delay(response, attempt)
                print(
                    f"Transient HTTP {response.status_code}: {path} params={params}; "
                    f"waiting about {delay:.0f}s before retry "
                    f"({attempt}/{MAX_ATTEMPTS})"
                )
                sleep_with_jitter(delay)
                continue

            response.raise_for_status()
            return response.json()

        except RequestException as exc:
            last_error = exc

            if attempt == MAX_ATTEMPTS:
                raise

            delay = min(60, 2 ** attempt)
            print(
                f"Request failed: {path} params={params}; {exc}. "
                f"Waiting about {delay:.0f}s before retry "
                f"({attempt}/{MAX_ATTEMPTS})"
            )
            sleep_with_jitter(delay)

    raise last_error


def get_or_fetch(cache_name, path, params=None):
    if USE_CACHE and not REFRESH_CACHE:
        cached = cache_read(cache_name)
        if cached is not None:
            print(f"Using cached response: {cache_name}")
            return cached

    data = get_json(path, params=params)
    cache_write(cache_name, data)

    if REQUEST_DELAY_SECONDS > 0:
        print(f"Pausing {REQUEST_DELAY_SECONDS:.1f}s before next FantasyPros request")
        time.sleep(REQUEST_DELAY_SECONDS)

    return data


def normalize_name(name):
    if not name:
        return ""

    value = name.lower().strip()
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    value = re.sub(r"\s+", " ", value)
    return value


def source_record(conn, source_name, source_key, payload, meta=None):
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
            "fetched_at": utcnow(),
            "content_hash": sha256_json(payload),
            "meta": json.dumps(meta or {}),
        },
    )


def fetch_rankings(scoring="PPR"):
    out = {}

    for position in RANKING_POSITIONS:
        cache_name = f"consensus-rankings_{scoring.lower()}_{position}.json"
        params = {
            "position": position.upper(),
            "scoring": scoring,
        }

        out[position] = get_or_fetch(
            cache_name,
            f"/nfl/{SEASON}/consensus-rankings",
            params=params,
        )

    return out


def fetch_projections():
    out = {}

    for position in PROJECTION_POSITIONS:
        cache_name = f"projections_{position}.json"
        params = {"position": position.upper()}

        out[position] = get_or_fetch(
            cache_name,
            f"/nfl/{SEASON}/projections",
            params=params,
        )

    return out


def find_best_player_match(conn, player_name, team, pos):
    norm_name = normalize_name(player_name)
    team_id = TEAM_MAP.get((team or "").upper())

    rows = conn.execute(
        text("""
            select id, player_name, pos, pro_team_id
            from players
            where platform = 'espn'
              and (
                lower(player_name) = :exact_name
                or lower(player_name) like :fuzzy_name
              )
            order by percent_owned desc nulls last, player_name asc
            limit 20
        """),
        {
            "exact_name": norm_name,
            "fuzzy_name": f"%{norm_name.split(' ')[0]}%" if norm_name else "%",
        },
    ).mappings().all()

    best = None
    best_score = -1

    for row in rows:
        score = 0
        row_name = normalize_name(row["player_name"])

        if row_name == norm_name:
            score += 70
        elif norm_name and row_name.startswith(norm_name.split(" ")[0]):
            score += 25

        if pos and row["pos"] and row["pos"].upper() == pos.upper():
            score += 20

        if team_id is not None and row["pro_team_id"] == team_id:
            score += 10

        if score > best_score:
            best_score = score
            best = dict(row)

    return best, float(best_score)


def upsert_xref(
    conn,
    player_id,
    source_player_key,
    source_name,
    source_player_name,
    source_team,
    source_pos,
    confidence,
    payload,
):
    conn.execute(
        text("""
            insert into player_xref (
              player_id, source_name, source_player_key, source_player_name,
              source_team, source_pos, confidence, payload
            ) values (
              :player_id, :source_name, :source_player_key, :source_player_name,
              :source_team, :source_pos, :confidence, cast(:payload as jsonb)
            )
            on conflict (source_name, source_player_key) do update set
              player_id = excluded.player_id,
              source_player_name = excluded.source_player_name,
              source_team = excluded.source_team,
              source_pos = excluded.source_pos,
              confidence = excluded.confidence,
              payload = excluded.payload
        """),
        {
            "player_id": player_id,
            "source_name": source_name,
            "source_player_key": source_player_key,
            "source_player_name": source_player_name,
            "source_team": source_team,
            "source_pos": source_pos,
            "confidence": confidence,
            "payload": json.dumps(payload),
        },
    )


def extract_items(payload):
    if isinstance(payload, dict):
        for key in ("players", "results", "data", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]

        if payload and all(isinstance(value, list) for value in payload.values()):
            merged = []
            for value in payload.values():
                merged.extend(value)
            return merged

    if isinstance(payload, list):
        return payload

    return []


def normalize_rankings(conn, ranking_payloads, scoring="PPR"):
    inserted = 0

    for position, payload in ranking_payloads.items():
        items = extract_items(payload)

        for item in items:
            name = item.get("player_name") or item.get("name") or item.get("player")
            team = item.get("team") or item.get("player_team")
            pos = item.get("player_position") or item.get("position") or position.upper()
            ecr = item.get("rank_ecr") or item.get("ecr") or item.get("rank")
            adp = item.get("adp") or item.get("rank_ave")
            tier = item.get("tier")

            source_key = str(
                item.get("player_id")
                or item.get("id")
                or f"{normalize_name(name)}|{team}|{pos}"
            )

            match, confidence = find_best_player_match(conn, name, team, pos)
            if not match:
                continue

            upsert_xref(
                conn=conn,
                player_id=match["id"],
                source_name="fantasypros",
                source_player_key=source_key,
                source_player_name=name,
                source_team=team,
                source_pos=pos,
                confidence=confidence,
                payload=item,
            )

            conn.execute(
                text("""
                    insert into projections (
                      player_id, source_name, season, scoring_format,
                      projected_points, adp, injury_status, news_summary, payload, fetched_at
                    ) values (
                      :player_id, 'fantasypros_ecr', :season, :scoring_format,
                      null, :adp, null, :news_summary, cast(:payload as jsonb), :fetched_at
                    )
                """),
                {
                    "player_id": match["id"],
                    "season": SEASON,
                    "scoring_format": scoring,
                    "adp": adp,
                    "news_summary": f"ECR={ecr}, tier={tier}, position={position}",
                    "payload": json.dumps(item),
                    "fetched_at": utcnow(),
                },
            )

            inserted += 1

        source_record(
            conn,
            "fantasypros",
            f"consensus-rankings:{scoring}:{position}:{SEASON}",
            payload,
            {
                "position": position,
                "scoring": scoring,
            },
        )

    return inserted


def normalize_projections(conn, projection_payloads):
    inserted = 0

    for position, payload in projection_payloads.items():
        items = extract_items(payload)

        for item in items:
            name = item.get("player_name") or item.get("name") or item.get("player")
            team = item.get("team") or item.get("player_team")
            pos = item.get("player_position") or item.get("position") or position.upper()
            proj_points = (
                item.get("projected_points")
                or item.get("points")
                or item.get("fantasy_points")
            )
            pass_yd = item.get("pass_yd") or item.get("passing_yards")
            rush_yd = item.get("rush_yd") or item.get("rushing_yards")
            rec_yd = item.get("rec_yd") or item.get("receiving_yards")
            receptions = item.get("receptions")
            injury_status = item.get("injury_status") or item.get("status")

            source_key = str(
                item.get("player_id")
                or item.get("id")
                or f"{normalize_name(name)}|{team}|{pos}"
            )

            match, confidence = find_best_player_match(conn, name, team, pos)
            if not match:
                continue

            upsert_xref(
                conn=conn,
                player_id=match["id"],
                source_name="fantasypros",
                source_player_key=source_key,
                source_player_name=name,
                source_team=team,
                source_pos=pos,
                confidence=confidence,
                payload=item,
            )

            conn.execute(
                text("""
                    insert into projections (
                      player_id, source_name, season, scoring_format,
                      projected_points, adp, receptions, pass_yd, rush_yd, rec_yd,
                      injury_status, news_summary, payload, fetched_at
                    ) values (
                      :player_id, 'fantasypros_proj', :season, 'PPR',
                      :projected_points, null, :receptions, :pass_yd, :rush_yd, :rec_yd,
                      :injury_status, null, cast(:payload as jsonb), :fetched_at
                    )
                """),
                {
                    "player_id": match["id"],
                    "season": SEASON,
                    "projected_points": proj_points,
                    "receptions": receptions,
                    "pass_yd": pass_yd,
                    "rush_yd": rush_yd,
                    "rec_yd": rec_yd,
                    "injury_status": injury_status,
                    "payload": json.dumps(item),
                    "fetched_at": utcnow(),
                },
            )

            inserted += 1

        source_record(
            conn,
            "fantasypros",
            f"projections:{position}:{SEASON}",
            payload,
            {"position": position},
        )

    return inserted


def main():
    if not API_KEY:
        raise SystemExit("Missing FANTASYPROS_API_KEY")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    print(
        f"Starting FantasyPros sync for {SEASON}; "
        f"cache={'enabled' if USE_CACHE else 'disabled'}, "
        f"refresh_cache={REFRESH_CACHE}, "
        f"request_delay={REQUEST_DELAY_SECONDS:.1f}s"
    )

    rankings = fetch_rankings(scoring="PPR")
    projections = fetch_projections()

    with engine.begin() as conn:
        ranking_count = normalize_rankings(conn, rankings, scoring="PPR")
        projection_count = normalize_projections(conn, projections)

    print(
        json.dumps(
            {
                "season": SEASON,
                "rankings_loaded": ranking_count,
                "projections_loaded": projection_count,
                "cache_dir": str(CACHE_ROOT),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
