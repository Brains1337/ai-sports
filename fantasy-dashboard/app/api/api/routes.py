from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("/")
def root():
    return {
        "service": "fantasy-dashboard",
        "endpoints": [
            "/health",
            "/leagues",
            "/players",
            "/rankings/latest",
            "/projections",
            "/roster-changes",
            "/roster-changes/summary",
            "/cfbd/players",
            "/leagues-members",
            "/league-members",
            "/roster-assignments",
        ],
    }


@router.get("/leagues")
def leagues(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        select
          id,
          external_league_id,
          platform,
          season,
          league_name,
          scoring_type,
          player_rank_type,
          scoring_enhancement_type,
          team_count,
          teams_joined,
          draft_type,
          time_per_selection,
          faab_budget,
          waiver_type,
          updated_at
        from leagues
        order by season desc, league_name
    """)).mappings().all()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get("/players")
def players(
    db: Session = Depends(get_db),
    pos: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    sql = """
        select
          id,
          platform,
          external_player_id,
          player_name,
          pos,
          pro_team_id,
          bye_week,
          eligible_slot_names,
          percent_owned
        from players
        where 1=1
    """
    params = {"limit": limit}
    if pos:
        sql += " and upper(pos) = upper(:pos)"
        params["pos"] = pos
    if search:
        sql += " and player_name ilike :search"
        params["search"] = f"%{search}%"
    sql += " order by percent_owned desc nulls last, player_name asc limit :limit"
    rows = db.execute(text(sql), params).mappings().all()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get("/projections")
def projections(
    db: Session = Depends(get_db),
    source: str | None = Query(
        default=None, description="Source name (e.g. cfbd_cfb_proj_yahoo)"
    ),
    season: int | None = Query(default=None, ge=2000, le=2100),
    week: int | None = Query(default=None, ge=0, le=25),
    pos: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    """
    Query fantasy projections from the `projections` table.

    Filters (all optional, AND-combined):
      - source   : CFBD source name (cfbd_cfb_proj_yahoo | cfbd_cfb_proj_fantrax)
      - season   : season year (e.g. 2026)
      - week     : week number (e.g. 2)
      - pos      : position filter (QB, RB, WR, TE, etc.)
    """
    sql = """
        select
          pr.id,
          pr.player_id,
          p.player_name,
          p.pos as position,
          p.pro_team_id as team,
          p.sport,
          pr.source_name,
          pr.season,
          pr.scoring_format,
          pr.week,
          pr.projected_points,
          pr.floor_points,
          pr.ceiling_points,
          (pr.payload->'stats'->>'pass_yd')::numeric as pass_yd,
          (pr.payload->'stats'->>'rush_yd')::numeric as rush_yd,
          (pr.payload->'stats'->>'rec_yd')::numeric as rec_yd,
          (pr.payload->'stats'->>'receptions')::numeric as receptions,
          pr.fetched_at
        from projections pr
        join players p on p.id = pr.player_id
        where 1=1
    """
    params: dict = {"limit": limit}
    if source:
        sql += " and pr.source_name = :source"
        params["source"] = source
    if season:
        sql += " and pr.season = :season"
        params["season"] = season
    if week is not None:
        sql += " and pr.week = :week"
        params["week"] = week
    if pos:
        sql += " and upper(p.pos) = upper(:pos)"
        params["pos"] = pos
    sql += " order by pr.projected_points desc nulls last limit :limit"
    rows = db.execute(text(sql), params).mappings().all()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get("/rankings/latest")
def rankings_latest(
    league_id: int = Query(...),
    limit: int = Query(default=50, ge=1, le=250),
    pos: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    league = (
        db.execute(
            text("select id, league_name from leagues where id = :league_id"),
            {"league_id": league_id},
        )
        .mappings()
        .first()
    )
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    sql = """
        with latest as (
          select max(created_at) as created_at
          from derived_rankings
          where league_id = :league_id
            and source_name = 'v1_model'
        )
        select
          dr.adjusted_rank,
          dr.adjusted_score,
          p.id as player_id,
          p.player_name,
          p.pos,
          p.pro_team_id,
          p.bye_week,
          p.percent_owned,
          dr.notes,
          dr.created_at
        from derived_rankings dr
        join latest l on l.created_at = dr.created_at
        join players p on p.id = dr.player_id
        where dr.league_id = :league_id
          and dr.source_name = 'v1_model'
    """
    params = {"league_id": league_id, "limit": limit}
    if pos:
        sql += " and upper(p.pos) = upper(:pos)"
        params["pos"] = pos
    sql += " order by dr.adjusted_rank asc limit :limit"

    rows = db.execute(text(sql), params).mappings().all()
    items = []
    for row in rows:
        item = dict(row)
        if item.get("notes"):
            try:
                item["notes"] = (
                    item["notes"]
                    if isinstance(item["notes"], dict)
                    else __import__("json").loads(item["notes"])
                )
            except Exception:
                pass
        items.append(item)

    return {
        "league": dict(league),
        "count": len(items),
        "items": items,
    }


# ── Roster Changes (season-tracking) ────────────────────────────────────────
# Powers the web UI that replaces manual roster_changes_report.sql runs.
# Reads from roster_status_changes (joined to players), same source of truth
# as fantasy-dashboard/sql/roster_changes_report.sql. [cite:177]


def _build_roster_changes_filters(
    pos: str | None,
    team: str | None,
    since: str | None,
    status_change: str,
    league_id: int | None,
    sport: str | None,
    params: dict,
) -> str:
    clauses: list[str] = []

    # League- or sport-scoped view
    if league_id:
        clauses.append("c.league_id = :league_id")
        params["league_id"] = league_id
    elif sport:
        clauses.append("p.sport = :sport")
        params["sport"] = sport

    if pos and pos.upper() != "ALL":
        clauses.append("upper(p.pos) = upper(:pos)")
        params["pos"] = pos

    if team:
        clauses.append("(c.previous_team = :team or c.current_team = :team)")
        params["team"] = team

    if since:
        clauses.append("c.latest_fetched_at >= :since")
        params["since"] = since

    # Drops: previously owned, now no fantasy team or FA/waivers.
    if status_change == "drops":
        clauses.append(
            "("
            "c.previous_status = 'owned' "
            "and (c.current_status in ('free_agent','waivers','unknown') "
            "     or c.current_status is null)"
            ")"
        )
    # Adds: previously FA/waivers/unknown, now owned.
    elif status_change == "adds":
        clauses.append(
            "("
            "c.current_status = 'owned' "
            "and (c.previous_status in ('free_agent','waivers','unknown') "
            "     or c.previous_status is null)"
            ")"
        )

    return f"where {' and '.join(clauses)}" if clauses else ""


def _sanitize_team_fields(item: dict) -> dict:
    """Clean up previous_team/current_team before returning to the UI.

    Some rows have stat strings (no letters) stored as team values.
    These are not real fantasy team names, so we treat any 'team'
    string with no letters as missing (None) to avoid 'owned · 1 1 6 8.00'
    in the UI.
    """
    for key in ("previous_team", "current_team"):
        val = item.get(key)
        if isinstance(val, str):
            if not any(ch.isalpha() for ch in val):
                item[key] = None
    return item


@router.get("/roster-changes")
def roster_changes(
    db: Session = Depends(get_db),
    status_change: str = Query(default="all", pattern="^(all|drops|adds)$"),
    pos: str | None = Query(default=None),
    team: str | None = Query(default=None),
    since: str | None = Query(default=None),
    league_id: int | None = Query(default=None),
    sport: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    params: dict = {}
    where_sql = _build_roster_changes_filters(
        pos, team, since, status_change, league_id, sport, params
    )

    count_sql = f"""
        select count(*) as total
        from roster_status_changes c
        join players p on p.id = c.player_id
        {where_sql}
    """
    total = db.execute(text(count_sql), params).scalar() or 0

    list_params = dict(params)
    list_params["limit"] = page_size
    list_params["offset"] = (page - 1) * page_size

    list_sql = f"""
        select
          p.id as player_id,
          p.player_name,
          p.pos,
          c.previous_status,
          c.current_status,
          c.previous_team,
          c.current_team,
          c.latest_fetched_at,
          c.league_id
        from roster_status_changes c
        join players p on p.id = c.player_id
        {where_sql}
        order by c.latest_fetched_at desc, p.pos, p.player_name
        limit :limit offset :offset
    """
    rows = db.execute(text(list_sql), list_params).mappings().all()

    items = [_sanitize_team_fields(dict(row)) for row in rows]

    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "total": total,
    }


@router.get("/roster-changes/summary")
def roster_changes_summary(
    db: Session = Depends(get_db),
    since: str | None = Query(default=None),
):
    params: dict = {}
    where_sql = ""
    if since:
        where_sql = "where c.latest_fetched_at >= :since"
        params["since"] = since

    sql = f"""
        select
          p.pos,
          c.current_status,
          count(*) as count
        from roster_status_changes c
        join players p on p.id = c.player_id
        {where_sql}
        group by p.pos, c.current_status
        order by p.pos, c.current_status
    """
    rows = db.execute(text(sql), params).mappings().all()
    return {"items": [dict(row) for row in rows]}


@router.get("/cfbd/players")
def cfbd_players(
    db: Session = Depends(get_db),
    season: int = Query(default=None, ge=2000, le=2100),
    team: str | None = Query(default=None),
    position: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """
    Query the cfbd_player_reference table — one row per CFBD athlete per season.

    Filters (all optional, AND-combined):
      - season   : CFBD season year (e.g. 2026)
      - team     : CFBD team name (e.g. 'Michigan')
      - position : position abbreviation (QB, RB, WR, TE, OL, etc.)
      - search   : free-text name search (ILIKE on full_name)
    """
    sql = """
        select
            athlete_id,
            first_name,
            last_name,
            full_name,
            position,
            team,
            team_id,
            conference,
            division,
            classification,
            abbreviation,
            school,
            height,
            weight,
            jersey,
            home_city,
            home_state,
            home_country,
            home_latitude,
            home_longitude,
            home_county_fips,
            recruit_ids,
            season,
            fetched_at
        from cfbd_player_reference
        where 1=1
    """
    params: dict = {"limit": limit}
    if season is not None:
        sql += " and season = :season"
        params["season"] = season
    if team:
        sql += " and team = :team"
        params["team"] = team
    if position:
        sql += " and position = :position"
        params["position"] = position
    if search:
        sql += " and full_name ilike :search"
        params["search"] = f"%{search}%"
    sql += " order by team, position, last_name, first_name limit :limit"
    rows = db.execute(text(sql), params).mappings().all()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get("/leagues-members")
def leagues_members(
    db: Session = Depends(get_db),
    platform: str | None = Query(default=None),
    manager_name: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """
    Query the leagues_members table — fantasy platform member/manager profiles.

    Filters (all optional, AND-combined):
      - platform      : e.g. 'yahoo-cfb', 'fantrax-cfb'
      - manager_name  : manager display name (ILIKE)
    """
    sql = """
        select
            id,
            platform,
            external_member_key,
            manager_name,
            manager_email,
            payload,
            created_at,
            updated_at
        from leagues_members
        where 1=1
    """
    params: dict = {"limit": limit}
    if platform:
        sql += " and platform = :platform"
        params["platform"] = platform
    if manager_name:
        sql += " and manager_name ilike :manager_name"
        params["manager_name"] = f"%{manager_name}%"
    sql += " order by platform, manager_name limit :limit"
    rows = db.execute(text(sql), params).mappings().all()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get("/league-members")
def league_members(
    db: Session = Depends(get_db),
    league_id: int | None = Query(default=None, ge=1),
    sport: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """
    Query the league_members table — which member manages which fantasy team.

    Filters (all optional, AND-combined):
      - league_id : internal leagues.id
      - sport     : filter by league sport (NFL, NCAAF)
    """
    sql = """
        select
            lm.id,
            lm.league_id,
            l.league_name,
            l.platform,
            l.sport,
            l.scoring_type,
            lm.member_id,
            lm.fantasy_team,
            lm.waiver_priority,
            lm.team_slot,
            lm.payload,
            lm.created_at,
            lm.updated_at,
            mem.platform as member_platform,
            mem.manager_name,
            mem.manager_email
        from league_members lm
        join leagues l on l.id = lm.league_id
        join leagues_members mem on mem.id = lm.member_id
        where 1=1
    """
    params: dict = {"limit": limit}
    if league_id is not None:
        sql += " and lm.league_id = :league_id"
        params["league_id"] = league_id
    if sport:
        sql += " and l.sport = :sport"
        params["sport"] = sport
    sql += " order by l.platform, l.league_name, lm.waiver_priority limit :limit"
    rows = db.execute(text(sql), params).mappings().all()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get("/roster-assignments")
def roster_assignments(
    db: Session = Depends(get_db),
    league_id: int | None = Query(default=None, ge=1),
    athlete_id: str | None = Query(default=None),
    player_id: int | None = Query(default=None),
    sport: str | None = Query(default=None),
    season: int | None = Query(default=None, ge=2000, le=2100),
    active_only: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
):
    """
    Query current/future roster assignments linking CFBD athletes to fantasy teams.

    Filters (all optional, AND-combined):
      - league_id    : internal leagues.id
      - athlete_id   : CFBD athlete ID (from cfbd_player_reference)
      - player_id    : internal players.id
      - sport        : NFL or NCAAF
      - season       : season year
      - active_only  : if true (default), only rows where valid_to IS NULL
    """
    sql = """
        select
            ra.id,
            ra.league_id,
            l.league_name,
            l.platform,
            ra.member_id,
            lm.fantasy_team,
            lm.waiver_priority,
            ra.athlete_id,
            p_cfbd.full_name as cfbd_player_name,
            p_cfbd.position as cfbd_position,
            p_cfbd.team as cfbd_team,
            ra.player_id,
            p.player_name,
            p.pos,
            ra.valid_from,
            ra.valid_to,
            ra.roster_status,
            ra.lineup_status,
            ra.slot_name,
            ra.season,
            ra.sport,
            ra.source_name
        from roster_assignments ra
        join leagues l on l.id = ra.league_id
        join league_members lm on lm.id = ra.member_id
        left join cfbd_player_reference p_cfbd
            on p_cfbd.athlete_id = ra.athlete_id
            and (p_cfbd.season = ra.season or ra.season is null)
        left join players p on p.id = ra.player_id
        where 1=1
    """
    params: dict = {"limit": limit}
    if league_id is not None:
        sql += " and ra.league_id = :league_id"
        params["league_id"] = league_id
    if athlete_id:
        sql += " and ra.athlete_id = :athlete_id"
        params["athlete_id"] = athlete_id
    if player_id is not None:
        sql += " and ra.player_id = :player_id"
        params["player_id"] = player_id
    if sport:
        sql += " and ra.sport = :sport"
        params["sport"] = sport
    if season is not None:
        sql += " and ra.season = :season"
        params["season"] = season
    if active_only:
        sql += " and ra.valid_to is null"
    sql += " order by ra.league_id, lm.waiver_priority, p.pos nulls last, p.player_name limit :limit"
    rows = db.execute(text(sql), params).mappings().all()
    return {"count": len(rows), "items": [dict(row) for row in rows]}
