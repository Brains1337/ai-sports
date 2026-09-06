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
            "/roster-changes",
            "/roster-changes/summary",
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
# Powers the web UI that replaces manual psql runs of roster_changes_report.sql.
# Reads from roster_status_changes (joined to players), same source of truth
# as fantasy-dashboard/sql/roster_changes_report.sql.

def _build_roster_changes_filters(
    pos: str | None,
    team: str | None,
    since: str | None,
    status_change: str,
    params: dict,
) -> str:
    clauses = []

    if pos and pos.upper() != "ALL":
        clauses.append("upper(p.pos) = upper(:pos)")
        params["pos"] = pos

    if team:
        clauses.append("(c.previous_team = :team or c.current_team = :team)")
        params["team"] = team

    if since:
        clauses.append("c.latest_fetched_at >= :since")
        params["since"] = since

    if status_change == "drops":
        clauses.append(
            "(c.current_status in ('free_agent','waivers') and c.previous_status = 'owned')"
        )
    elif status_change == "adds":
        clauses.append(
            "(c.previous_status in ('free_agent','waivers') and c.current_status = 'owned')"
        )

    return f"where {' and '.join(clauses)}" if clauses else ""


@router.get("/roster-changes")
def roster_changes(
    db: Session = Depends(get_db),
    status_change: str = Query(default="all", pattern="^(all|drops|adds)$"),
    pos: str | None = Query(default=None),
    team: str | None = Query(default=None),
    since: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    params: dict = {}
    where_sql = _build_roster_changes_filters(pos, team, since, status_change, params)

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
          c.latest_fetched_at
        from roster_status_changes c
        join players p on p.id = c.player_id
        {where_sql}
        order by c.latest_fetched_at desc, p.pos, p.player_name
        limit :limit offset :offset
    """
    rows = db.execute(text(list_sql), list_params).mappings().all()

    return {
        "items": [dict(row) for row in rows],
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
