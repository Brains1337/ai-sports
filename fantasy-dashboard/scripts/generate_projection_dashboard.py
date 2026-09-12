#!/usr/bin/env python3
"""
generate_projection_dashboard.py

Fetches live CFBD projections and Yahoo CFB roster data from the fantasy API,
then generates a human-readable HTML dashboard with:
  - Start/sit recommendations (team-aware using MY_TEAM_NAME env)
  - Drop candidates (players on roster with better free agents available)
  - Top pickup targets
  - Full roster view with projections

Usage:
    python3 generate_projection_dashboard.py [week] [--team "Team Name"]

Environment:
    MY_TEAM_NAME  - Yahoo CFB team name (default: "Venables Vengeance")
    API_BASE      - Fantasy API base URL (default: https://fantasy-api.ai.goffs.xyz)
    OUTPUT        - Output HTML path (default: projections_dashboard.html)
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime

BASE = os.environ.get("API_BASE", "https://fantasy-api.ai.goffs.xyz")
TEAM_NAME = os.environ.get("MY_TEAM_NAME", "Venables Vengeance")
OUTPUT = os.environ.get("OUTPUT", "projections_dashboard.html")
SCORING_FORMAT = "HALF_PPR"


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
    if p.get("rec_yd", 0) > 0:
        parts.append(f"{p['rec_yd']:.0f} rec")
    if p.get("receptions", 0) > 0:
        parts.append(f"{p['receptions']:.0f} cat")
    return " · ".join(parts) if parts else "No stats"


def pos_badge(pos: str) -> str:
    classes = {
        "QB": "pos-qb", "RB": "pos-rb", "WR": "pos-wr",
        "TE": "pos-te", "K": "pos-k", "DEF": "pos-def",
    }
    return f'<span class="pos-badge {classes.get(pos, "")}">{pos}</span>'


def status_badge(action: str) -> str:
    labels = {"START": "START", "FLEX": "FLEX", "BENCH": "BENCH"}
    return f'<span class="status-badge {action.lower()}">{labels.get(action, "BENCH")}</span>'


def build_dashboard(week: int = 2) -> str:
    """Build the full HTML dashboard from live API data."""
    all_projections = fetch_api(
        "/projections",
        f"source=cfbd_cfb_proj_yahoo&week={week}&limit=500&sort_dir=desc",
    )

    roster = fetch_api("/roster-changes", f"team={urllib.parse.quote(TEAM_NAME)}")

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
                "player_id": proj["player_id"],
            })
        elif not proj:
            unmatched.append({"name": name, "pos": pos})

    # Available free agents
    my_pids = {p["player_id"] for p in my_players}
    available = []
    for proj in all_projections:
        if proj["player_id"] not in my_pids:
            available.append({
                "name": proj["player_name"],
                "pos": proj.get("pos", proj.get("position", "?")),
                "projected_points": proj.get("projected_points", 0) or 0,
                "pass_yd": proj.get("pass_yd", 0) or 0,
                "rush_yd": proj.get("rush_yd", 0) or 0,
                "rec_yd": proj.get("rec_yd", 0) or 0,
                "receptions": proj.get("receptions", 0) or 0,
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
        players = sorted(my_by_pos.get(pos, []), key=lambda x: x["projected_points"], reverse=True)
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
    for p in my_players:
        if starters_map.get(p["name"]) == "BENCH":
            pos = p["pos"]
            avail_pos = avail_by_pos.get(pos, [])
            if avail_pos and avail_pos[0]["projected_points"] > p["projected_points"] * 1.4:
                drop_candidates.append({
                    "player": p,
                    "better": avail_pos[0],
                    "gap": avail_pos[0]["projected_points"] - p["projected_points"],
                })

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
    starters = [p for p in my_players if starters_map.get(p["name"]) in ("START", "FLEX")]
    lineup_total = sum(p["projected_points"] for p in starters)

    # --- HTML ---
    h = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Week {week} CFB Fantasy Dashboard — {TEAM_NAME}</title>
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
.grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:20px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;box-shadow:var(--shadow)}}
.card h3{{font-size:16px;margin-bottom:14px;color:var(--fg)}}
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
@media(max-width:768px){{.header{{padding:24px 28px}}.header h1{{font-size:22px}}.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🏈 Week {week} CFB Projections</h1>
    <div class="subtitle">{TEAM_NAME}</div>
    <div class="meta">CFBD defense-adjusted projections • {datetime.now().strftime("%B %d, %Y")}</div>
    <div class="legend"><span><span class="dot" style="background:#10b981"></span>START</span><span><span class="dot" style="background:#2563eb"></span>FLEX</span><span><span class="dot" style="background:#6b7280"></span>BENCH</span></div>
  </div>

  <div class="card" style="margin-bottom:24px;text-align:center">
    <h3 style="text-align:center">Projected Starters Total</h3>
    <div class="total-points">{lineup_total:.1f} pts</div>
    <div class="total-label">Week {week} Starters Projection</div>
  </div>
'''

    # Position groups
    h += '<div class="grid" style="margin-bottom:24px">'
    for pos in ["QB", "RB", "WR", "TE"]:
        players = sorted(my_by_pos.get(pos, []), key=lambda x: x["projected_points"], reverse=True)
        h += f'<div class="card" style="flex:1;min-width:140px"><h3>{pos}</h3><table><tbody>'
        for p in players:
            h += f'<tr><td><strong>{p["name"]}</strong><br><span class="metric">{stat_detail(p)}</span></td><td class="points">{p["projected_points"]:.1f}</td><td>{status_badge(starters_map.get(p["name"],"BENCH"))}</td></tr>'
        h += '</tbody></table></div>'

    # K
    h += '<div class="card" style="flex:1;min-width:140px"><h3>K</h3><table><tbody>'
    for p in my_by_pos.get("K", []):
        h += f'<tr><td><strong>{p["name"]}</strong></td><td class="points">{p["projected_points"]:.1f}</td><td>{status_badge("START")}</td></tr>'
    for u in unmatched:
        if u["pos"] == "K":
            h += f'<tr><td><strong style="opacity:.5">{u["name"]}</strong></td><td class="points">—</td><td><span class="status-badge bench">NO DATA</span></td></tr>'
    h += '</tbody></table></div>'

    # DEF
    h += '<div class="card" style="flex:1;min-width:140px"><h3>DEF</h3><table><tbody>'
    for p in my_by_pos.get("DEF", []):
        h += f'<tr><td><strong>{p["name"]}</strong></td><td class="points">{p["projected_points"]:.1f}</td><td>{status_badge("START")}</td></tr>'
    for u in unmatched:
        if u["pos"] == "DEF":
            h += f'<tr><td><strong style="opacity:.5">{u["name"]}</strong></td><td class="points">—</td><td><span class="status-badge bench">NO DATA</span></td></tr>'
    h += '</tbody></table></div></div>'

    # Drop candidates + Top pickups
    h += '<div class="grid" style="margin-bottom:24px">'
    h += '<div class="card" style="flex:1"><h3>📉 Drop Candidates</h3>'
    if drop_candidates:
        h += '<table><thead><tr><th>Player</th><th>Proj</th><th>Available FA</th></tr></thead><tbody>'
        for dc in sorted(drop_candidates, key=lambda x: x["gap"], reverse=True)[:6]:
            p, b = dc["player"], dc["better"]
            h += f'<tr><td><strong>{p["name"]}</strong>{pos_badge(p["pos"])}<br><span class="metric">{stat_detail(p)}</span></td><td class="points">{p["projected_points"]:.1f}</td><td><strong>{b["name"]}</strong> ({b["projected_points"]:.1f})</td></tr>'
        h += '</tbody></table>'
    else:
        h += '<p class="metric">No strong drop candidates — your bench depth is solid.</p>'
    h += '</div>'

    h += '<div class="card" style="flex:1"><h3>📈 Top Pickups</h3><table><thead><tr><th>Player</th><th>Pos</th><th>Proj</th></tr></thead><tbody>'
    for p in top_pickups[:8]:
        h += f'<tr><td><strong>{p["name"]}</strong><br><span class="metric">{stat_detail(p)}</span></td><td>{pos_badge(p["pos"])}</td><td class="points">{p["projected_points"]:.1f}</td></tr>'
    h += '</tbody></table></div></div>'

    # Full roster
    h += f'<div class="card" style="margin-bottom:24px"><h3>Full Roster — {len(my_players)} matched · {len(unmatched)} no projection data</h3>'
    h += '<table><thead><tr><th>Player</th><th>Pos</th><th>Proj Pts</th><th>Details</th><th>Status</th></tr></thead><tbody>'
    for p in sorted(my_players, key=lambda x: (x["pos"], -x["projected_points"])):
        h += f'<tr><td>{p["name"]}</td><td>{pos_badge(p["pos"])}</td><td class="points">{p["projected_points"]:.1f}</td><td class="metric">{stat_detail(p)}</td><td>{status_badge(starters_map.get(p["name"],"BENCH"))}</td></tr>'
    h += '</tbody></table></div>'

    # Best available by position
    h += '<div class="grid">'
    for pos in ["QB", "RB", "WR", "TE"]:
        avail_pos = avail_by_pos.get(pos, [])[:5]
        if avail_pos:
            h += f'<div class="card"><h3>Best Available {pos}</h3><table><thead><tr><th>Player</th><th>Proj</th></tr></thead><tbody>'
            for p in avail_pos:
                h += f'<tr><td><strong>{p["name"]}</strong><br><span class="metric">{stat_detail(p)}</span></td><td class="points">{p["projected_points"]:.1f}</td></tr>'
            h += '</tbody></table></div>'
    h += '</div>'

    h += f'''
  <div class="footer">Generated from <a href="{BASE}/projections">API</a> | CFBD defense-adjusted | {len(all_projections)} players projected for Week {week}</div>
</div>
</body>
</html>'''
    return h


if __name__ == "__main__":
    import urllib.parse  # need this import for the f-string above

    week = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    html = build_dashboard(week)

    with open(OUTPUT, "w") as f:
        f.write(html)

    print(f"Dashboard written to {OUTPUT} ({len(html)} bytes)")
    print(f"Team: {TEAM_NAME} | Week: {week}")
