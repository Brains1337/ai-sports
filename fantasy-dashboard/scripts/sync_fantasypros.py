#!/usr/bin/env python3
"""sync_fantasypros.py — NFL projections, ECR and news from FantasyPros.

Rewritten 2026-09-08. What was wrong with the previous version:

* **Only 47 players ever loaded**, against 415 rostered players across the
  two ESPN leagues, and *zero* of those 47 overlapped with a rostered
  player. The matcher compared a punctuation-stripped needle against an
  unstripped column, fell through to ``LIKE '%firstname%'`` ordered by
  ``percent_owned``, and could never return "no match" — so 94 provider
  keys bound onto 47 arbitrary rows. Now uses ``common.matching``, which
  refuses below a confidence floor.
* **No ``week`` parameter was ever sent.** FantasyPros treats ``week`` as an
  optional query param with ``week=0`` meaning season-long, so the old code
  could only ever retrieve season totals — making weekly start/sit
  impossible regardless of schema.
* **``scoring_format`` was hardcoded ``'PPR'``** while these four leagues
  span STD, HALF_PPR and PPR. The projections endpoint returns all three
  point totals in one response, so we now write all three from a single
  call rather than burning three times the quota.
* **Blind ``INSERT``** with no unique key, so every run appended duplicates.
  Now upserts on ``ux_projections_key`` (migration 011).
* **ECR was stuffed into ``news_summary``** as ``"ECR=12, tier=3, ..."`` and
  re-parsed downstream. Now uses the ``ecr_rank`` / ``ecr_tier`` columns.
* **``platform='espn'`` was hardcoded** — migration 010 renamed that to
  ``espn-nfl``, so the old script would now match nothing at all.

Run diagnostics first if the API shape is uncertain:

    python sync_fantasypros.py --diagnose
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.constants import (  # noqa: E402
    SEASON_LONG_WEEK,
    Platform,
    ScoringType,
    SourceName,
    Sport,
    canonical_pos,
)
from common.db import begin, run_tracked  # noqa: E402
from common.http import CallBudget, JsonClient, env_flag, record_source, utcnow  # noqa: E402
from common.matching import (  # noqa: E402
    MatchStats,
    PlayerMatcher,
    find_collapses,
    upsert_xref,
)

SCRIPT = "sync_fantasypros"

SEASON = int(os.getenv("FANTASY_SEASON", os.getenv("FP_SEASON", os.getenv("ESPN_SEASON", "2026"))))
CURRENT_WEEK = int(os.getenv("FANTASY_WEEK", "1"))
API_KEY = os.getenv("FANTASYPROS_API_KEY", "").strip()

#: Base URL candidates, tried in order.
#:
#: /public/v2/json is FIRST because it is empirically known to work with this
#: key — the previous version of this script used it and did return data.
#: The current public docs say /v2/json, which returned 403 for this key on
#: 2026-09-08, so the docs likely describe a different (paid) tier.
#: Override with FP_BASE_URL to pin one explicitly.
_DEFAULT_BASES = [
    "https://api.fantasypros.com/public/v2/json",
    "https://api.fantasypros.com/v2/json",
]
BASE_URL_CANDIDATES = (
    [os.environ["FP_BASE_URL"]] + _DEFAULT_BASES
    if os.getenv("FP_BASE_URL")
    else _DEFAULT_BASES
)

CACHE_DIR = Path(os.getenv("FP_CACHE_DIR", "/data/cache/fantasypros")) / str(SEASON)

#: FantasyPros position codes. Team defense is DST on their side, DEF here.
FP_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"]

#: Which weeks to pull. 0 = season-long, plus the live week.
def weeks_to_sync() -> list[int]:
    raw = os.getenv("FP_WEEKS", "").strip()
    if raw:
        return sorted({int(w) for w in raw.split(",") if w.strip()})
    return sorted({SEASON_LONG_WEEK, CURRENT_WEEK})


# Alternate spellings seen across FantasyPros endpoints. Their payload keys
# are not perfectly stable between rankings and projections, hence aliases.
FIELD_ALIASES = {
    "name": ("player_name", "name", "player", "player_filename"),
    "team": ("player_team_id", "team", "player_team", "team_id"),
    "pos": ("player_position_id", "player_position", "position", "pos"),
    "fp_id": ("fpid", "player_id", "id", "player_fpid"),
    "points_std": ("points", "fpts", "points_std"),
    "points_half": ("points_half", "points_half_ppr", "fpts_half"),
    "points_ppr": ("points_ppr", "fpts_ppr"),
    "adp": ("adp", "rank_ave", "avg"),
    "ecr": ("rank_ecr", "ecr", "rank"),
    "tier": ("tier", "player_tier"),
    "injury": ("player_injury_status", "injury_status", "status"),
    "pass_yd": ("pass_yds", "pass_yd", "passing_yards"),
    "pass_td": ("pass_tds", "pass_td", "passing_tds"),
    "rush_yd": ("rush_yds", "rush_yd", "rushing_yards"),
    "rec_yd": ("rec_yds", "rec_yd", "receiving_yards"),
    "rec": ("rec", "receptions"),
}


def pick(item: dict, field: str) -> Any:
    """First non-empty value among the known aliases for a logical field."""
    for key in FIELD_ALIASES.get(field, (field,)):
        val = item.get(key)
        if val not in (None, "", "-"):
            return val
    return None


def as_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    f = as_float(value)
    return int(f) if f is not None else None


def extract_items(payload: Any) -> list[dict]:
    """Pull the player array out of a FantasyPros response."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("players", "results", "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        # Some endpoints nest per-position lists under arbitrary keys.
        merged: list[dict] = []
        for val in payload.values():
            if isinstance(val, list):
                merged.extend(x for x in val if isinstance(x, dict))
        return merged
    return []


def make_client() -> JsonClient:
    budget = CallBudget("fantasypros", limit=int(os.getenv("FP_CALL_LIMIT", "300")))
    return JsonClient(
        base_url=BASE_URL_CANDIDATES[0],
        headers={"x-api-key": API_KEY},
        cache_dir=CACHE_DIR,
        use_cache=env_flag("FP_USE_CACHE", True),
        refresh_cache=env_flag("FP_REFRESH_CACHE", False),
        # Projections change daily; a 6h TTL stops a stale cache pinning
        # 9-day-old data the way the old unbounded cache did.
        cache_ttl_seconds=int(os.getenv("FP_CACHE_TTL", str(6 * 3600))),
        request_delay=float(os.getenv("FP_REQUEST_DELAY_SECONDS", "1.5")),
        budget=budget,
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def diagnose(client: JsonClient) -> int:
    """Probe the API and print the real response shape.

    Worth running before trusting a field map: the alias table above is
    defensive precisely because the payload keys are undocumented.

    Probes a matrix of base URL x endpoint x params, because a 403 is
    ambiguous — it can mean the wrong path, a key not provisioned for that
    endpoint, or a season the tier does not cover. Testing the axes
    separately tells them apart.
    """
    print("== probe matrix ==")
    print(f"   season={SEASON}  key={'set (%d chars)' % len(API_KEY)}")

    # Vary one axis at a time so the failing dimension is identifiable.
    probes = [
        ("projections, week=0", f"nfl/{SEASON}/projections", {"position": "RB", "week": 0}),
        ("projections, no week", f"nfl/{SEASON}/projections", {"position": "RB"}),
        ("consensus-rankings", f"nfl/{SEASON}/consensus-rankings",
         {"position": "RB", "scoring": "PPR"}),
        ("players (no season)", "nfl/players", None),
        (f"projections {SEASON - 1}", f"nfl/{SEASON - 1}/projections",
         {"position": "RB", "week": 0}),
    ]

    working: tuple[str, str, dict | None] | None = None
    for cand in BASE_URL_CANDIDATES:
        client.base_url = cand.rstrip("/")
        print(f"\n-- base: {cand}")
        for label, path, prm in probes:
            try:
                client.get(path, prm, max_attempts=1)
                print(f"   OK    {label}")
                if working is None:
                    working = (cand, path, prm)
            except Exception as exc:  # noqa: BLE001 - probing on purpose
                status = getattr(exc, "status", None)
                body = getattr(exc, "body", "") or ""
                detail = f"HTTP {status}" if status else type(exc).__name__
                print(f"   FAIL  {label}: {detail}")
                if body.strip():
                    print(f"           {body[:220]}")

    if working is None:
        print(
            "\nNothing worked. Most likely causes, in order:\n"
            "  1. The key is not provisioned for the projections endpoint\n"
            "     (free tier is licensed for non-production prototyping).\n"
            f"  2. Season {SEASON} is not available on this tier — check\n"
            "     whether the previous season responds above.\n"
            "  3. The key is stale or has a typo.\n"
            "The raw payloads from the last successful run are already in\n"
            "projections.payload, so field discovery can proceed without the\n"
            "API. See the query in the handover notes.",
            file=sys.stderr,
        )
        return 1

    base, _, _ = working
    client.base_url = base.rstrip("/")
    print(f"\n== using base: {base} ==")

    for endpoint, prm in (
        (f"nfl/{SEASON}/projections", {"position": "RB", "week": 0}),
        (f"nfl/{SEASON}/consensus-rankings", {"position": "RB", "scoring": "PPR"}),
    ):
        print(f"\n== {endpoint} {prm} ==")
        try:
            data = client.get(endpoint, prm)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {exc}")
            continue
        if isinstance(data, dict):
            print(f"  top-level keys: {sorted(data.keys())}")
            for k in ("season", "week", "count", "positions", "scoring"):
                if k in data:
                    print(f"    {k} = {data[k]!r}")
        items = extract_items(data)
        print(f"  items: {len(items)}")
        if items:
            print(f"  sample item keys: {sorted(items[0].keys())}")
            print("  sample item:")
            print(json.dumps(items[0], indent=4)[:1800])
            print("\n  resolved via alias map:")
            for f in ("name", "team", "pos", "fp_id", "points_std",
                      "points_half", "points_ppr", "adp", "ecr", "injury"):
                print(f"    {f:12s} -> {pick(items[0], f)!r}")
    return 0


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

PROJ_UPSERT = text(
    """
    insert into projections (
        player_id, source_name, season, week, scoring_format,
        projected_points, adp, receptions, pass_yd, rush_yd, rec_yd,
        injury_status, ecr_rank, ecr_tier, opportunity, confidence,
        payload, fetched_at
    ) values (
        :player_id, :source_name, :season, :week, :scoring_format,
        :projected_points, :adp, :receptions, :pass_yd, :rush_yd, :rec_yd,
        :injury_status, :ecr_rank, :ecr_tier, :opportunity, :confidence,
        cast(:payload as jsonb), :fetched_at
    )
    on conflict (player_id, source_name, season, week,
                 coalesce(scoring_format, ''))
    do update set
        projected_points = excluded.projected_points,
        adp              = coalesce(excluded.adp, projections.adp),
        receptions       = excluded.receptions,
        pass_yd          = excluded.pass_yd,
        rush_yd          = excluded.rush_yd,
        rec_yd           = excluded.rec_yd,
        injury_status    = excluded.injury_status,
        ecr_rank         = coalesce(excluded.ecr_rank, projections.ecr_rank),
        ecr_tier         = coalesce(excluded.ecr_tier, projections.ecr_tier),
        opportunity      = excluded.opportunity,
        confidence       = excluded.confidence,
        payload          = excluded.payload,
        fetched_at       = excluded.fetched_at
    """
)


def sync_projections(
    conn: Any,
    client: JsonClient,
    matcher: PlayerMatcher,
    stats: Any,
    mstats: MatchStats,
) -> None:
    """Projections for every position x week, all three scoring formats.

    One API call yields STD, HALF_PPR and PPR point totals, so we fan out to
    three rows per player rather than making three calls — the free tier is
    quota-sensitive and these leagues genuinely need all three formats.
    """
    fetched_at = utcnow()

    for week in weeks_to_sync():
        for fp_pos in FP_POSITIONS:
            params = {"position": fp_pos, "week": week}
            key = f"projections:{SEASON}:w{week}:{fp_pos}"
            try:
                payload = client.get(f"nfl/{SEASON}/projections", params)
            except Exception as exc:  # noqa: BLE001
                print(f"  {key} FAILED: {exc}", file=sys.stderr)
                record_source(
                    conn,
                    source_name=SourceName.FANTASYPROS,
                    source_key=key,
                    payload={"error": str(exc)},
                    status="error",
                )
                stats.bump("fetch_errors")
                continue

            items = extract_items(payload)
            record_source(
                conn,
                source_name=SourceName.FANTASYPROS,
                source_key=key,
                payload=payload,
                meta={"position": fp_pos, "week": week, "items": len(items)},
            )
            if not items:
                stats.bump("empty_responses")
                print(f"  {key}: 0 items")
                continue

            canon_pos = canonical_pos(fp_pos)

            for item in items:
                name = pick(item, "name")
                if not name:
                    stats.add(skipped=1)
                    stats.bump("skip_no_name")
                    continue

                team = pick(item, "team")
                item_pos = canonical_pos(pick(item, "pos")) or canon_pos

                match = matcher.match_or_refuse(str(name), mstats, pos=item_pos)
                if match is None:
                    stats.add(skipped=1)
                    stats.bump("skip_unmatched")
                    continue

                fp_id = pick(item, "fp_id")
                source_key = str(fp_id) if fp_id else f"{name}|{team}|{item_pos}"

                upsert_xref(
                    conn,
                    player_id=match.player_id,
                    source_name=SourceName.FANTASYPROS,
                    source_player_key=source_key,
                    source_player_name=str(name),
                    source_team=str(team) if team else None,
                    source_pos=item_pos,
                    confidence=match.confidence,
                    payload=json.dumps(item, default=str),
                    match_tier=match.tier,
                )

                rec = as_float(pick(item, "rec"))
                pts = {
                    ScoringType.STD: as_float(pick(item, "points_std")),
                    ScoringType.HALF_PPR: as_float(pick(item, "points_half")),
                    ScoringType.PPR: as_float(pick(item, "points_ppr")),
                }

                # If the payload only carries one total, derive the others
                # from reception count rather than storing wrong values.
                if pts[ScoringType.PPR] is None and pts[ScoringType.STD] is not None \
                        and rec is not None:
                    pts[ScoringType.PPR] = pts[ScoringType.STD] + rec
                if pts[ScoringType.HALF_PPR] is None and pts[ScoringType.STD] is not None \
                        and rec is not None:
                    pts[ScoringType.HALF_PPR] = pts[ScoringType.STD] + 0.5 * rec

                wrote_any = False
                for fmt, value in pts.items():
                    if value is None:
                        continue
                    conn.execute(
                        PROJ_UPSERT,
                        {
                            "player_id": match.player_id,
                            "source_name": str(SourceName.FANTASYPROS_PROJ),
                            "season": SEASON,
                            "week": week,
                            "scoring_format": str(fmt),
                            "projected_points": value,
                            "adp": as_float(pick(item, "adp")),
                            "receptions": rec,
                            "pass_yd": as_float(pick(item, "pass_yd")),
                            "rush_yd": as_float(pick(item, "rush_yd")),
                            "rec_yd": as_float(pick(item, "rec_yd")),
                            "injury_status": pick(item, "injury"),
                            "ecr_rank": None,
                            "ecr_tier": None,
                            # Touches/targets — the volume signal the
                            # projection model needs, not just the total.
                            "opportunity": _opportunity(item, item_pos),
                            "confidence": match.confidence,
                            "payload": json.dumps(item, default=str),
                            "fetched_at": fetched_at,
                        },
                    )
                    stats.add(written=1)
                    wrote_any = True

                if not wrote_any:
                    stats.add(skipped=1)
                    stats.bump("skip_no_points")

            print(f"  {key}: {len(items)} items")


def _opportunity(item: dict, pos: str | None) -> float | None:
    """Expected touches (RB) or targets (WR/TE) — the volume driver."""
    if pos in ("WR", "TE"):
        return as_float(item.get("rec_tgt") or item.get("targets") or pick(item, "rec"))
    if pos == "RB":
        rush = as_float(item.get("rush_att") or item.get("rush_atts")) or 0.0
        rec = as_float(pick(item, "rec")) or 0.0
        total = rush + rec
        return total or None
    return None


def sync_rankings(
    conn: Any,
    client: JsonClient,
    matcher: PlayerMatcher,
    stats: Any,
    mstats: MatchStats,
) -> None:
    """Consensus ECR + ADP, per scoring format.

    Written onto the *same* rows as the projections via the ``ecr_rank`` /
    ``ecr_tier`` columns, so there is no longer a parallel
    ``fantasypros_ecr`` source to keep in step.
    """
    fetched_at = utcnow()

    for scoring in (ScoringType.STD, ScoringType.HALF_PPR, ScoringType.PPR):
        for fp_pos in FP_POSITIONS:
            params = {"position": fp_pos, "scoring": str(scoring)}
            key = f"consensus-rankings:{SEASON}:{scoring}:{fp_pos}"
            try:
                payload = client.get(f"nfl/{SEASON}/consensus-rankings", params)
            except Exception as exc:  # noqa: BLE001
                print(f"  {key} FAILED: {exc}", file=sys.stderr)
                stats.bump("fetch_errors")
                continue

            items = extract_items(payload)
            record_source(
                conn,
                source_name=SourceName.FANTASYPROS,
                source_key=key,
                payload=payload,
                meta={"position": fp_pos, "scoring": str(scoring), "items": len(items)},
            )
            if not items:
                stats.bump("empty_responses")
                continue

            canon = canonical_pos(fp_pos)

            for item in items:
                name = pick(item, "name")
                if not name:
                    continue
                item_pos = canonical_pos(pick(item, "pos")) or canon
                match = matcher.match_or_refuse(str(name), mstats, pos=item_pos)
                if match is None:
                    stats.bump("ecr_unmatched")
                    continue

                ecr = as_int(pick(item, "ecr"))
                tier = as_int(pick(item, "tier"))
                adp = as_float(pick(item, "adp"))
                if ecr is None and adp is None:
                    continue

                # Attach to the season-long row; ECR is a season-long concept.
                updated = conn.execute(
                    text(
                        """
                        update projections
                           set ecr_rank = coalesce(:ecr, ecr_rank),
                               ecr_tier = coalesce(:tier, ecr_tier),
                               adp      = coalesce(:adp, adp)
                         where player_id      = :player_id
                           and source_name    = :source_name
                           and season         = :season
                           and week           = :week
                           and scoring_format = :scoring
                        """
                    ),
                    {
                        "ecr": ecr,
                        "tier": tier,
                        "adp": adp,
                        "player_id": match.player_id,
                        "source_name": str(SourceName.FANTASYPROS_PROJ),
                        "season": SEASON,
                        "week": SEASON_LONG_WEEK,
                        "scoring": str(scoring),
                    },
                ).rowcount

                if updated:
                    stats.bump("ecr_applied")
                else:
                    # Ranked but not projected — keep it, the planner can
                    # still use ECR as a fallback signal.
                    conn.execute(
                        PROJ_UPSERT,
                        {
                            "player_id": match.player_id,
                            "source_name": str(SourceName.FANTASYPROS_PROJ),
                            "season": SEASON,
                            "week": SEASON_LONG_WEEK,
                            "scoring_format": str(scoring),
                            "projected_points": None,
                            "adp": adp,
                            "receptions": None,
                            "pass_yd": None,
                            "rush_yd": None,
                            "rec_yd": None,
                            "injury_status": pick(item, "injury"),
                            "ecr_rank": ecr,
                            "ecr_tier": tier,
                            "opportunity": None,
                            "confidence": match.confidence,
                            "payload": json.dumps(item, default=str),
                            "fetched_at": fetched_at,
                        },
                    )
                    stats.add(written=1)
                    stats.bump("ecr_only_rows")


def sync_injuries(conn: Any, stats: Any) -> None:
    """Promote the freshest injury status onto ``players.injury_status``.

    Only runs once migration 016 has added the column; skipped silently
    otherwise so this script stays runnable mid-migration.
    """
    has_col = conn.execute(
        text(
            """
            select 1 from information_schema.columns
             where table_name='players' and column_name='injury_status'
            """
        )
    ).scalar_one_or_none()
    if not has_col:
        stats.bump("injury_skipped_no_column")
        return

    n = conn.execute(
        text(
            """
            update players p
               set injury_status     = src.injury_status,
                   injury_updated_at = now()
              from (
                select distinct on (player_id) player_id, injury_status
                  from projections
                 where source_name = :source_name
                   and season      = :season
                   and injury_status is not null
                 order by player_id, fetched_at desc
              ) src
             where p.id = src.player_id
               and coalesce(p.injury_status,'') <> coalesce(src.injury_status,'')
            """
        ),
        {"source_name": str(SourceName.FANTASYPROS_PROJ), "season": SEASON},
    ).rowcount
    stats.bump("injury_status_updated", n or 0)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--diagnose", action="store_true",
                    help="probe the API and print response shapes, write nothing")
    ap.add_argument("--skip-rankings", action="store_true")
    args = ap.parse_args()

    if not API_KEY:
        print("FANTASYPROS_API_KEY is not set", file=sys.stderr)
        return 1

    client = make_client()

    if args.diagnose:
        return diagnose(client)

    base = client.probe_base_url(
        f"nfl/{SEASON}/projections", {"position": "RB", "week": 0}, BASE_URL_CANDIDATES
    )
    if not base:
        print("Could not reach the FantasyPros API with this key.", file=sys.stderr)
        return 1

    print(f"FantasyPros sync: season={SEASON} weeks={weeks_to_sync()} base={base}")

    with run_tracked(SCRIPT, min_rows=1) as stats:
        mstats = MatchStats()
        with begin() as conn:
            matcher = PlayerMatcher(conn, Platform.ESPN_NFL, Sport.NFL)
            print(f"  matcher loaded {len(matcher)} {Platform.ESPN_NFL} players")
            if len(matcher) == 0:
                # Fail loudly. Silently matching nothing is how this script
                # produced 47 rows and looked healthy.
                raise SystemExit(
                    f"No players found for platform={Platform.ESPN_NFL}. "
                    "Has migration 010 been applied?"
                )

            stats.note("players_in_pool", len(matcher))

            print("== projections ==")
            sync_projections(conn, client, matcher, stats, mstats)

            if not args.skip_rankings:
                print("== consensus rankings ==")
                sync_rankings(conn, client, matcher, stats, mstats)

            sync_injuries(conn, stats)

            stats.meta.update(mstats.as_meta())
            stats.meta.update(client.budget.as_meta())

            collapses = find_collapses(conn, str(SourceName.FANTASYPROS))
            if collapses:
                worst = collapses[:5]
                stats.note("xref_collapses", len(collapses))
                stats.note("xref_collapse_worst", worst)
                print(
                    f"  WARNING: {len(collapses)} player_id(s) have multiple "
                    f"FantasyPros keys mapped to them; worst={worst}",
                    file=sys.stderr,
                )
            else:
                stats.note("xref_collapses", 0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
