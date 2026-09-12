## Project: AI Sports — Fantasy Football Intelligence Platform

GitLab: https://gitlab.com/ai-platform4863732/ai-sports | GitHub mirror: https://github.com/Brains1337/ai-sports

You have ReadOnly access to the project files here: /home/hermes/projects/fantasy_league_management/ai-sports

## Active Leagues
**NFL (ESPN):** TX Guillotine (L01): https://fantasy.espn.com/football/team?leagueId=209442251&teamId=16&seasonId=2026 | The_Dark_Side Guillotine (L02): https://fantasy.espn.com/football/team?leagueId=719429857&teamId=15&seasonId=2026 | Pick'em Bear Down in Dallas: https://fantasy.espn.com/games/nfl-pickem-2026/group?id=47911333-fe56-4a18-8389-9b76314c323a
**NCAAF:** Yahoo EDIT League (L01): https://college.fantasysports.yahoo.com/cfb/37494/4 | Fantrax New Freshman (L02): https://www.fantrax.com/fantasy/league/2hbybmp6msnsbuqa/home

## Tech Stack
- Backend: Python FastAPI :8000 | Frontend: Node.js/Express :3000 (draft-dashboard.html)
- DB: PostgreSQL 15 :5432 | Orchestration: Docker Compose/Dockge :5001 | NPM: Verdaccio :4873
- CI/CD: GitLab CI (.gitlab-ci.yml)
- Providers: ESPN API (ESPN_S2/SWID), Fantrax API, FantasyPros scraping, Yahoo Sports API
- Scripts: sync_espn.py, sync_fantasypros.py, build_rankings.py, fantrax-draft-poller.mjs, quick_draft_poll.py

## Core Principles
1. **DB first** — All player data, status history, news, rankings, waivers, rosters in PostgreSQL. Extend fantasy-dashboard/sql/init.sql for every schema change.
2. **Automate everything** — Cron/pollers/scrapers on schedules; nothing requires manual intervention.
3. **Scrape proactively** — Rotowire, ESPN, PFF, CBS Sports, NFL injury reports before API updates reflect changes.
4. **Hourly monitoring** — All rostered/watchlist/waiver players checked hourly. Alerts fire immediately on change.
5. **Telegram alerts** — Injury, start/sit, waivers, opponent moves, pick'em. Every alert includes context + action.
6. **Beat waivers** — Rank targets before they hit waivers. Factor FAAB/priority. Alert on high-value drops.
7. **Opponent intel** — Track all teams: roster, adds/drops, trades, start/sit patterns.

## Data Schema
- **leagues**: provider, league_id, team_id, sport, scoring_type, waiver_type, platform
- **players**: player_id, name, team, position, sport, provider_id, status, depth_chart_position, last_updated
- **player_events**: timestamp, player_id, headline, source_url, impact (1–5), alert_sent
- **rosters**: per-league/team snapshots with timestamps for opponent diff-ing
- **waiver_targets**: projected_pts, availability, priority_score, claim_recommendation
- **rankings**: FantasyPros ECR + composite score; tagged by week, sport, scoring format
- **matchup_schedule**: weekly opponent + defensive strength for start/sit
- **pickem**: weekly picks, confidence %, home/away factor, streak

## Automation Schedule
| Job | Frequency | Action |
|---|---|---|
| Player status monitor | Hourly | Scrape ESPN/Rotowire/PFF; diff status; fire Telegram on change |
| League sync | Every 2h | Pull rosters ESPN/Yahoo/Fantrax; diff opponent moves; alert |
| Waiver planner | Daily (Wed/Thu) | Rank available players by pts+opportunity; push top 5 to Telegram |
| Rankings rebuild | Nightly | Run build_rankings.py; flag ±10 rank moves |
| Pick'em analysis | Weekly (Tue) | NFL schedule + lines + weather → pick confidence % |
| PFF / CBS scrape | Every 2h | Grades, snap counts, news |
| 247Sports / Rivals | Every 4h | NCAAF depth charts, player news |

## Telegram Alerts
🚨 INJURY — [Player] now [OUT/IR/Q]: [headline]. Action: [drop/stream/hold]
📈 WAIVER — [Player] free in [League]. Proj PPG: [X]. Claim over [Y].
🔄 OPPONENT — [Team] added [Player] in [League]. Adjust waiver strategy.
🏆 START/SIT — Week [N] [League]: Start [A] over [B]. Edge: [reason].
📊 RANK — [Player] moved [X]→[Y] on FantasyPros. [Context].
🏈 PICK'EM — Week [N]: [Team] over [Team]. Conf: [%]. Line: [spread].
⚠️ DEPTH — [Player] dropped to [2nd/3rd] string. Impact: [H/M/L].

## Dev Guidelines
- Branch from main; open MR; CI must pass before merge
- Secrets in .env (ESPN_S2, SWID, Telegram token, Yahoo OAuth, Fantrax tokens) — never commit
- DB migrations: fantasy-dashboard/sql/002_*.sql incremental files
- Scrapers: rate limit respect + exponential backoff retry
- All pollers in Docker containers via fantasy-dashboard/compose.yaml
- Python APIs → fantasy-dashboard/app/api/routes.py | Node utils → app/dashboard/ + lib/providers/
- Use unified players table with sport field for NFL/NCAAF cross-league reuse
- Structured JSON logs for all scraping + API calls
- **Python style**: Black/PEP 8 (4-space indent). All code in fantasy-dashboard/app/api must pass black --check. Note when edits require running black fantasy-dashboard/app/api/ before commit.

## Build Priority
1. Telegram bot service — Docker container, typed alert messages
2. Player status monitor — hourly Rotowire+ESPN scraper → player_events → Telegram
3. Roster sync — ESPN/Yahoo/Fantrax every 2h, diff snapshots
4. Waiver planner — scoring model, top 5 picks to Telegram
5. Opponent tracker — dashboard view + add/drop alerts
6. Pick'em analyzer — weekly NFL picks for Bear Down in Dallas
7. NCAAF scrapers — 247Sports/ESPN depth charts for Yahoo EDIT + Fantrax New Freshman
