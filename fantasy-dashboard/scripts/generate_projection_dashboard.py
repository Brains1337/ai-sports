#!/usr/bin/env python3
"""
generate_projection_dashboard.py

Fetches live CFBD projections and roster data from the fantasy API,
then generates a human-readable HTML dashboard with:
  - Start/sit recommendations (team-aware using MY_TEAM_NAME env)
  - Drop candidates (players on roster with better free agents available)
  - Top pickup targets
  - Full roster view with projections
  - Best available players by position

Usage:
    python3 generate_projection_dashboard.py [--platform yahoo|fantrax|espn]
                                             [--week N] [--output-dir DIR]
                                             [--scoring-format HALF_PPR|PPR|STD]

Environment:
    MY_TEAM_NAME  - Team name (default: "Venables Vengeance")
    API_BASE      - Fantasy API base URL (default: https://fantasy-api.ai.goffs.xyz)

Platform source mapping:
    yahoo   -> cfbd_cfb_proj_yahoo  (NCAAF Yahoo CFB projections)
    fantrax -> cfbd_cfb_proj_fantrax (NCAAF Fantrax projections)
    espn    -> espn_nfl_proj         (NFL ESPN projections)
"""
import argparse
import html as html_lib
import json
import os
import urllib.request
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime

BASE = os.environ.get("API_BASE", "https://fantasy-api.ai.goffs.xyz")
TEAM_NAME = os.environ.get("MY_TEAM_NAME", "Venables Vengeance")

PLATFORM_SOURCES = {
    "yahoo": "cfbd_cfb_proj_yahoo",
    "fantrax": "cfbd_cfb_proj_fantrax",
    "espn": "espn_nfl_proj",
}


def fetch_api(path: str, params: str = "") -> list:
    """Fetch JSON from the API and return the items list."""
    url = f"{BASE}{path}?{params}" if params else f"{BASE}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    if isinstance(data, dict):
        return data.get("items", data.get("data", []))
    return data


def name_variants(name: str) -> set:
    """Generate normalized name variants for fuzzy matching across data sources."""
    name = name.strip()
    if "," in name:
        last, first = name.split(",", 1)
        last = last.strip().lower()
        first = first.strip().lower()
        return {f"{first} {last}", f"{last} {first}"}
    tokens = name.lower().split()
    variants = set()
    if len(tokens) >= 2:
        variants.add(" ".join(tokens))
        variants.add(" ".join(reversed(tokens)))
    else:
        variants.add(name.lower())
    variants.add(" ".join(sorted(tokens)))
    return variants


def stat_detail(p: dict) -> str:
    """Format player stats for display."""
    parts = []
    if p.get("pass_yd", 0) > 0:
        parts.append(f"{p['pass_yd']:.0f} pass")
    if p.get("rush_yd", 0) > 0:
        parts.append(f"{p['rush_yd']:.0f} rush")
    if p.get("receptions", 0) > 0:
        parts.append(f"{p['receptions']:.1f} tgt")
    if p.get("rec_yd", 0) > 0:
        parts.append(f"{p['rec_yd']:.0f} rec")
    if p.get("pass_td", 0) > 0:
        parts.append(f"{p['pass_td']:.0f} pTD")
    if p.get("rush_td", 0) > 0:
        parts.append(f"{p['rush_td']:.0f} rTD")
    if p.get("rec_td", 0) > 0:
        parts.append(f"{p['rec_td']:.0f} recTD")
    return " · ".join(parts) if parts else "No stats"


def pos_badge(pos: str) -> str:
    classes = {
        "QB": "pos-qb", "RB": "pos-rb", "WR": "pos-wr",
        "TE": "pos-te", "K": "pos-k", "DEF": "pos-def",
    }
    cls = classes.get(pos, "")
    return f'<span class="pos-badge {cls}">{pos}</span>'


def status_badge(action: str) -> str:
    labels = {"START": "START", "FLEX": "FLEX", "BENCH": "BENCH"}
    cls = action.lower()
    label = labels.get(action, "BENCH")
    return f'<span class="status-badge {cls}">{label}</span>'


def player_row(p: dict, starters_map: dict) -> str:
    """Generate a roster table row for a player."""
    name = p["name"]
    status = starters_map.get(name, "BENCH")
    return (
        f'<tr><td>{name}</td><td>{pos_badge(p["pos"])}</td>'
        f'<td class="points">{p["projected_points"]:.1f}</td>'
        f'<td class="metric">{stat_detail(p)}</td>'
        f'<td>{status_badge(status)}</td></tr>'
    )


def pos_table_row(p: dict, starters_map: dict) -> str:
    """Generate a position-group table row for a player."""
    name = p["name"]
    status = starters_map.get(name, "BENCH")
    return (
        f'<tr><td><strong>{name}</strong><br>'
        f'<span class="metric">{stat_detail(p)}</span></td>'
        f'<td class="points">{p["projected_points"]:.1f}</td>'
        f'<td>{status_badge(status)}</td></tr>'
    )


def no_data_row(name: str, pos: str = "") -> str:
    """Generate a row for players with no projection data."""
    pos_html = f'<span class="pos-badge pos-{pos.lower()}">{pos}</span>' if pos else ""
    return (
        f'<tr><td><strong style="opacity:.5">{name}</strong></td>'
        f'<td>{pos_html}</td>'
        f'<td class="points">—</td>'
        f'<td class="metric">No projection data for this week</td>'
        f'<td><span class="status-badge bench">NO DATA</span></td></tr>'
    )


def drop_candidate_row(dc: dict) -> str:
    """Generate a drop candidate table row."""
    p = dc["player"]
    b = dc["better"]
    return (
        f'<tr><td><strong>{p["name"]}</strong>{pos_badge(p["pos"])}<br>'
        f'<span class="metric">{stat_detail(p)}</span></td>'
        f'<td class="points">{p["projected_points"]:.1f}</td>'
        f'<td><strong>{b["name"]}</strong> ({b["projected_points"]:.1f})</td></tr>'
    )


def pickup_row(p: dict) -> str:
    """Generate a top pickup table row."""
    return (
        f'<tr><td><strong>{p["name"]}</strong><br>'
        f'<span class="metric">{stat_detail(p)}</span></td>'
        f'<td>{pos_badge(p["pos"])}</td>'
        f'<td class="points">{p["projected_points"]:.1f}</td></tr>'
    )


def best_available_row(p: dict) -> str:
    """Generate a best available table row."""
    return (
        f'<tr><td><strong>{p["name"]}</strong><br>'
        f'<span class="metric">{stat_detail(p)}</span></td>'
        f'<td class="points">{p["projected_points"]:.1f}</td></tr>'
    )


def pos_group_html(pos: str, players: list, starters_map: dict, include_badge: bool = False) -> str:
    """Generate a position group card with a table of players.

    Args:
        pos: Position label (e.g. "QB", "K", "DEF")
        players: List of player dicts
        starters_map: Dict mapping player name to starter status
        include_badge: If True, show status badge (for QB/RB/WR/TE position groups)
                       If False (for K/DEF), always show START
    """
    rows = []
    for p in sorted(players, key=lambda x: x["projected_points"], reverse=True):
        if include_badge:
            rows.append(pos_table_row(p, starters_map))
        else:
            name = p["name"]
            rows.append(
                f'<tr><td><strong>{name}</strong></td>'
                f'<td class="points">{p["projected_points"]:.1f}</td>'
                f'<td>{status_badge("START")}</td></tr>'
            )
    return (
        f'<div class="card">'
        f'<h3>{pos}</h3><table><tbody>'
        f'{"".join(rows)}'
        f'</tbody></table></div>'
    )


def build_dashboard(week: int, platform: str, scoring_format: str = "HALF_PPR") -> str:
    """Build the full HTML dashboard from live API data.

    Args:
        week: NFL/NCAAF week number
        platform: 'yahoo', 'fantrax', or 'espn'
        scoring_format: 'HALF_PPR', 'PPR', or 'STD'
    """
    source = PLATFORM_SOURCES[platform]

    all_projections = fetch_api(
        "/projections",
        f"source={source}&week={week}&scoring_format={scoring_format}&limit=500&sort_dir=desc",
    )

    roster = fetch_api("/roster-changes", f"team={urllib.parse.quote(TEAM_NAME)}")

    # Fetch ALL rostered players across the league to exclude from free agents
    # The no-team-filter call returns only the latest ~50 changes, so we need
    # to discover team names and query each individually.
    all_rostered = fetch_api("/roster-changes", "limit=500")
    # Clean HTML entities from team names
    all_team_names: set[str] = set()
    for item in all_rostered:
        team = item.get("current_team") or ""
        team = html_lib.unescape(team.strip()) if team else ""
        if team and not team.startswith("W ") and not team.startswith("L "):
            all_team_names.add(team)

    all_owned_pids: set = set()
    # Build name-to-pid map from projections for fast name matching
    proj_name_to_pid: dict[str, int] = {}
    for p in all_projections:
        for variant in name_variants(p["player_name"]):
            proj_name_to_pid[variant] = p["player_id"]

    for team_name in all_team_names:
        team_roster = fetch_api("/roster-changes", f"team={urllib.parse.quote(team_name)}")
        for item in team_roster:
            if item.get("current_status") == "owned":
                # Skip corrupted comma-format names from sync bug
                if "," in item.get("player_name", ""):
                    continue
                all_owned_pids.add(item["player_id"])
                # Also match by name for players not matched by PID
                for variant in name_variants(item["player_name"]):
                    if variant in proj_name_to_pid:
                        all_owned_pids.add(proj_name_to_pid[variant])

    # Build lookup maps
    proj_by_pid = {p["player_id"]: p for p in all_projections}
    proj_by_name: dict[str, dict] = {}
    for p in all_projections:
        for variant in name_variants(p["player_name"]):
            proj_by_name[variant] = p

    # Match rostered players, deduping by projection player_id
    my_players = []
    unmatched = []
    seen_proj_pids = set()

    for item in roster:
        pid = item["player_id"]
        name = item["player_name"]
        pos = item["pos"]

        # Skip corrupted roster entries from sync bug where player_name
        # is in "Lastname, Firstname" comma format. The real Yahoo CFB
        # roster always returns "First Last" format. Comma-format names
        # are duplicate entries created by the name-based fallback upsert
        # path in sync_yahoo.py, which assigned players to wrong teams.
        if "," in name:
            continue

        # Skip players whose name is too short to be a real player
        # name (e.g. "ND" scraped from team abbreviation). Real player
        # names are always full names with at least 2 characters in
        # both first and last name portions.
        if len(name.split()) < 2 or any(len(w) < 3 for w in name.split()):
            continue

        # Skip players no longer on the team (current_team is NULL)
        if item.get("current_team") != TEAM_NAME:
            continue

        proj = proj_by_pid.get(pid)
        if not proj:
            for variant in name_variants(name):
                if variant in proj_by_name:
                    proj = proj_by_name[variant]
                    break

        if proj and proj["player_id"] not in seen_proj_pids:
            seen_proj_pids.add(proj["player_id"])
            my_players.append({
                "name": proj["player_name"],
                "pos": pos,
                "projected_points": proj.get("projected_points", 0) or 0,
                "pass_yd": proj.get("pass_yd", 0) or 0,
                "rush_yd": proj.get("rush_yd", 0) or 0,
                "rec_yd": proj.get("rec_yd", 0) or 0,
                "receptions": proj.get("receptions", 0) or 0,
                "pass_td": proj.get("pass_td", 0) or 0,
                "rush_td": proj.get("rush_td", 0) or 0,
                "rec_td": proj.get("rec_td", 0) or 0,
                "player_id": proj["player_id"],
            })
        elif not proj:
            unmatched.append({"name": name, "pos": pos})

    # Available free agents (exclude all rostered players, not just your team)
    my_pids = {p["player_id"] for p in my_players}
    excluded_pids = my_pids | all_owned_pids
    available = []
    for proj in all_projections:
        if proj["player_id"] not in excluded_pids:
            available.append({
                "name": proj["player_name"],
                "pos": proj.get("pos", proj.get("position", "?")),
                "projected_points": proj.get("projected_points", 0) or 0,
                "pass_yd": proj.get("pass_yd", 0) or 0,
                "rush_yd": proj.get("rush_yd", 0) or 0,
                "rec_yd": proj.get("rec_yd", 0) or 0,
                "receptions": proj.get("receptions", 0) or 0,
                "pass_td": proj.get("pass_td", 0) or 0,
                "rush_td": proj.get("rush_td", 0) or 0,
                "rec_td": proj.get("rec_td", 0) or 0,
                "player_id": proj["player_id"],
            })

    # Group by position
    my_by_pos: dict[str, list] = defaultdict(list)
    for p in my_players:
        my_by_pos[p["pos"]].append(p)

    avail_by_pos: dict[str, list] = defaultdict(list)
    for p in available:
        avail_by_pos[p["pos"]].append(p)
    for pos_list in avail_by_pos.values():
        pos_list.sort(key=lambda x: x["projected_points"], reverse=True)

    # Determine starters: QB(1), RB(2), WR(2), TE(1), FLEX, K(1), DEF(1)
    start_counts = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    starters_map: dict[str, str] = {}
    bench_by_pos: dict[str, list] = defaultdict(list)

    for pos in ["QB", "RB", "WR", "TE"]:
        players = sorted(
            my_by_pos.get(pos, []),
            key=lambda x: x["projected_points"],
            reverse=True,
        )
        for p in players[: start_counts[pos]]:
            starters_map[p["name"]] = "START"
        for p in players[start_counts[pos] :]:
            starters_map[p["name"]] = "BENCH"
            bench_by_pos[pos].append(p)

    # Flex: best bench player from RB/WR/TE
    flex_candidates = []
    for pos in ["RB", "WR", "TE"]:
        flex_candidates.extend(bench_by_pos[pos])
    flex_candidates.sort(key=lambda x: x["projected_points"], reverse=True)
    if flex_candidates:
        starters_map[flex_candidates[0]["name"]] = "FLEX"

    for p in my_by_pos.get("K", []):
        starters_map[p["name"]] = "START"
    for p in my_by_pos.get("DEF", []):
        starters_map[p["name"]] = "START"

    # Drop candidates
    drop_candidates = []
    used_fas: set[str] = set()
    for p in my_players:
        if starters_map.get(p["name"]) == "BENCH":
            pos = p["pos"]
            avail_pos = avail_by_pos.get(pos, [])
            if avail_pos:
                for fa in avail_pos:
                    if fa["name"] in used_fas:
                        continue
                    if fa["projected_points"] > p["projected_points"] * 1.4:
                        drop_candidates.append({
                            "player": p,
                            "better": fa,
                            "gap": fa["projected_points"] - p["projected_points"],
                        })
                        used_fas.add(fa["name"])
                        break

    # Top pickups
    top_pickups = []
    seen_names = set()
    for pos in ["QB", "RB", "WR", "TE"]:
        for p in avail_by_pos.get(pos, []):
            if p["name"] not in seen_names:
                top_pickups.append(p)
                seen_names.add(p["name"])
                break
    for p in sorted(available, key=lambda x: x["projected_points"], reverse=True):
        if p["name"] not in seen_names and len(top_pickups) < 8:
            top_pickups.append(p)
            seen_names.add(p["name"])

    # Lineup total
    starters = [
        p for p in my_players
        if starters_map.get(p["name"]) in ("START", "FLEX")
    ]
    lineup_total = sum(p["projected_points"] for p in starters)

    # --- Build HTML sections ---

    # Position group cards
    pos_group_cards = ""
    for pos in ["QB", "RB", "WR", "TE"]:
        pos_group_cards += pos_group_html(pos, my_by_pos.get(pos, []), starters_map, include_badge=True)

    # K card with no-data rows for unmatched
    k_rows = []
    for p in sorted(my_by_pos.get("K", []), key=lambda x: x["projected_points"], reverse=True):
        name = p["name"]
        k_rows.append(
            f'<tr><td><strong>{name}</strong></td>'
            f'<td class="points">{p["projected_points"]:.1f}</td>'
            f'<td>{status_badge("START")}</td></tr>'
        )
    for u in unmatched:
        if u["pos"] == "K":
            k_rows.append(no_data_row(u["name"], "K"))
    k_card = (
        f'<div class="card">'
        f'<h3>K</h3><table><tbody>{"".join(k_rows)}</tbody></table></div>'
    )

    # DEF card with no-data rows for unmatched
    def_rows = []
    for p in sorted(my_by_pos.get("DEF", []), key=lambda x: x["projected_points"], reverse=True):
        name = p["name"]
        def_rows.append(
            f'<tr><td><strong>{name}</strong></td>'
            f'<td class="points">{p["projected_points"]:.1f}</td>'
            f'<td>{status_badge("START")}</td></tr>'
        )
    for u in unmatched:
        if u["pos"] == "DEF":
            def_rows.append(no_data_row(u["name"], "DEF"))
    def_card = (
        f'<div class="card">'
        f'<h3>DEF</h3><table><tbody>{"".join(def_rows)}</tbody></table></div>'
    )

    # Drop candidates section
    if drop_candidates:
        dc_rows = "".join(drop_candidate_row(dc) for dc in sorted(drop_candidates, key=lambda x: x["gap"], reverse=True)[:6])
        drop_html = f'<table><thead><tr><th>Player</th><th>Proj</th><th>Available FA</th></tr></thead><tbody>{dc_rows}</tbody></table>'
    else:
        drop_html = '<p class="metric">No strong drop candidates — your bench depth is solid.</p>'

    # Top pickups section
    pickup_rows = "".join(pickup_row(p) for p in top_pickups[:8])
    pickup_html = f'<table><thead><tr><th>Player</th><th>Pos</th><th>Proj</th></tr></thead><tbody>{pickup_rows}</tbody></table>'

    # Full roster table (matched + unmatched)
    unmatched_rows = "".join(
        no_data_row(u["name"], u["pos"])
        for u in unmatched
    )
    roster_rows = "".join(
        player_row(p, starters_map)
        for p in sorted(my_players, key=lambda x: (x["pos"], -x["projected_points"]))
    ) + unmatched_rows

    # Best available by position
    best_available_cards = ""
    for pos in ["QB", "RB", "WR", "TE"]:
        avail_pos = avail_by_pos.get(pos, [])[:5]
        if avail_pos:
            rows = "".join(best_available_row(p) for p in avail_pos)
            best_available_cards += (
                f'<div class="card">'
                f'<h3>Best Available {pos}</h3>'
                f'<table><thead><tr><th>Player</th><th>Proj</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>'
            )

    timestamp = datetime.now().strftime("%B %d, %Y")
    platform_upper = platform.upper()

    # --- Assemble final HTML ---
    h = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Week {week} Fantasy Dashboard — {TEAM_NAME}</title>
<link rel="icon" href="data:,">
<style>
:root {{ --fg:#1a1a2e;--muted:#6b7280;--accent:#2563eb;--border:#d1d5db;--card:#fff;--bg:#f5f7fa;--success:#10b981;--warning:#f59e0b;--danger:#ef4444;--qb:#3b82f6;--rb:#10b981;--wr:#f59e0b;--te:#8b5cf6;--shadow:0 1px 3px rgba(0,0,0,.08); }}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--fg);line-height:1.6;padding:24px}}
.container{{max-width:1100px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#1e3a8a 0%,#3730a3 100%);color:#fff;padding:36px 40px;border-radius:20px;margin-bottom:24px;box-shadow:var(--shadow)}}
.header h1{{font-size:28px;margin-bottom:4px}}
.header .subtitle{{opacity:.9;font-size:16px;font-weight:500}}
.header .meta{{opacity:.8;font-size:13px;margin-top:10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:20px}}
        .grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;box-shadow:var(--shadow)}}
.card h3{{font-size:16px;margin-bottom:14px;color:var(--fg)}}
.card h2{{font-size:18px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}}
th{{background:rgba(0,0,0,.03);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}}
tr:hover td{{background:rgba(0,0,0,.02)}}
.pos-badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase}}
.pos-qb{{background:rgba(59,130,246,.15);color:var(--qb)}}
.pos-rb{{background:rgba(16,185,130,.15);color:var(--rb)}}
.pos-wr{{background:rgba(245,158,11,.15);color:var(--wr)}}
.pos-te{{background:rgba(139,92,246,.15);color:var(--te)}}
.pos-k{{background:rgba(100,116,128,.15);color:#647489}}
.pos-def{{background:rgba(239,68,68,.15);color:var(--danger)}}
.status-badge{{display:inline-block;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:600}}
.start{{background:rgba(16,185,130,.15);color:var(--success)}}
.flex{{background:rgba(37,99,235,.15);color:var(--accent)}}
.bench{{background:rgba(100,116,128,.15);color:#6b7280}}
.points{{font-weight:700}}
.metric{{color:var(--muted);font-size:12.5px}}
.total-points{{font-size:28px;font-weight:800;color:var(--accent)}}
.total-label{{font-size:12px;color:var(--muted);text-transform:uppercase}}
.legend{{display:flex;gap:20px;margin-top:10px;font-size:12px}}
.legend span{{display:inline-flex;align-items:center;gap:4px}}
.legend .dot{{width:8px;height:8px;border-radius:50%;display:inline-block}}
.footer{{text-align:center;color:var(--muted);font-size:12px;margin-top:30px}}
@media(max-width:768px){{.header{{padding:24px 28px}}.header h1{{font-size:22px}}.grid{{grid-template-columns:1fr}}.grid-2{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <h1>🏈 Week {week} Fantasy Projections</h1>
    <div class="subtitle">{TEAM_NAME}</div>
    <div class="meta">{platform_upper} · {scoring_format} · CFBD defense-adjusted projections</div>
    <div class="legend">
      <span><span class="dot" style="background:#10b981"></span>START</span>
      <span><span class="dot" style="background:#2563eb"></span>FLEX</span>
      <span><span class="dot" style="background:#6b7280"></span>BENCH</span>
    </div>
  </div>

  <!-- Lineup Total -->
  <div class="card" style="margin-bottom:24px;text-align:center">
    <h3 style="text-align:center">Projected Starters Total</h3>
    <div class="total-points">{lineup_total:.1f} pts</div>
    <div class="total-label">Week {week} Starters Projection</div>
  </div>

  <!-- Position Groups -->
  <div class="grid" style="margin-bottom:24px">
    {pos_group_cards}
    {k_card}
    {def_card}
  </div>

  <!-- Drop Candidates + Top Pickups -->
  <div class="grid-2" style="margin-bottom:24px">
    <div class="card">
      <h3>📉 Drop Candidates</h3>
      {drop_html}
    </div>
    <div class="card">
      <h3>📈 Top Pickups</h3>
      {pickup_html}
    </div>
  </div>

  <!-- Full Roster -->
  <div class="card" style="margin-bottom:24px">
    <h3>Full Roster — {len(my_players)} matched · {len(unmatched)} no projection data</h3>
    <table>
      <thead><tr><th>Player</th><th>Pos</th><th>Proj Pts</th><th>Details</th><th>Status</th></tr></thead>
      <tbody>
        {roster_rows}
      </tbody>
    </table>
  </div>

  <!-- Best Available by Position -->
  <div class="grid">
    {best_available_cards}
  </div>

  <div class="footer">
    Generated from <a href="{BASE}/projections">API</a> · {platform_upper} ·
    {len(all_projections)} players projected · Week {week} · {timestamp}
  </div>

</div>
</body>
</html>'''
    return h


def main():
    parser = argparse.ArgumentParser(
        description="Generate HTML fantasy projection dashboard with sit/drop/pickup recommendations"
    )
    parser.add_argument(
        "--platform", "-p",
        choices=["yahoo", "fantrax", "espn"],
        default="yahoo",
        help="Fantasy platform (default: yahoo)",
    )
    parser.add_argument(
        "--week", "-w",
        type=int,
        default=2,
        help="Week number (default: 2)",
    )
    parser.add_argument(
        "--output-dir", "-d",
        default=".",
        help="Output directory for the HTML file (default: current directory)",
    )
    parser.add_argument(
        "--scoring-format", "-s",
        choices=["HALF_PPR", "PPR", "STD"],
        default="HALF_PPR",
        help="Scoring format (default: HALF_PPR)",
    )
    parser.add_argument(
        "--team", "-t",
        default=None,
        help="Override team name (default: $MY_TEAM_NAME or 'Venables Vengeance')",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Override output filename (default: {platform}_week{week}_dashboard.html)",
    )
    args = parser.parse_args()

    # Override team name if specified
    global TEAM_NAME
    if args.team:
        TEAM_NAME = args.team

    # Determine output filename
    if args.output:
        filename = args.output
    else:
        filename = f"{args.platform}_week{args.week}_dashboard.html"

    output_path = os.path.join(args.output_dir, filename)

    # Build and save dashboard
    html = build_dashboard(args.week, args.platform, args.scoring_format)

    os.makedirs(args.output_dir, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)

    print(f"Dashboard written to {output_path} ({len(html)} bytes)")
    print(f"Team: {TEAM_NAME} | Platform: {args.platform} | Week: {args.week} | Scoring: {args.scoring_format}")


if __name__ == "__main__":
    main()
