from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_db

router = APIRouter()

@router.get("/")
def root():
    return {
        "service": "fantasy-dashboard",
        "endpoints": [
            "/health",
            "/leagues",
            "/players",
            "/rankings/latest"
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
    league = db.execute(text("select id, league_name from leagues where id = :league_id"), {"league_id": league_id}).mappings().first()
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
                item["notes"] = item["notes"] if isinstance(item["notes"], dict) else __import__("json").loads(item["notes"])
            except Exception:
                pass
        items.append(item)

    return {
        "league": dict(league),
        "count": len(items),
        "items": items,
    }

