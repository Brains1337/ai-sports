# IDEA.md — Hermes: Intelligent Alert & Notification Service

> **Hermes** is the message-delivery backbone of the AI Sports platform.
> Named for the messenger of the gods, it is the single outbound channel
> through which every automated insight — injury alerts, waiver targets,
> opponent moves, rank shifts, start/sit calls, and pick'em picks — reaches
> the operator via Telegram.

---

## Vision

Every data pipeline in the platform is useless unless actionable intelligence
reaches the right person at the right moment. Hermes closes that loop. It
consumes structured events from PostgreSQL, enriches them with context, and
fires typed Telegram messages — formatted, prioritized, and deduplicated —
before any competitor reacts.

Hermes is **not** a generic notifier. Every alert carries:
- What happened (event + player + league)
- Why it matters (impact score, projected points delta)
- What to do (hold / drop / claim / start / sit)

---

## Core Responsibilities

| Responsibility | Detail |
|---|---|
| Typed alert dispatch | One message template per alert class; no freeform strings |
| Deduplication | `alert_sent` flag on `player_events`; no double-fires |
| Priority routing | Impact 4–5 → immediate; impact 1–3 → batched hourly digest |
| Multi-league awareness | Every alert scoped to league name + platform |
| Retry + backoff | Telegram 429 / network errors retried with exponential backoff |
| Structured logging | JSON logs per alert: type, player, league, timestamp, delivery status |

---

## Alert Taxonomy

All alert types the system must support, mapped to Telegram emoji prefix:

| Prefix | Type | Trigger Source |
|---|---|---|
| 🚨 INJURY | Player status changed to OUT / IR / Q | `player_events` impact ≥ 3 |
| 📈 WAIVER | High-value player available in a league | `waiver_targets` priority_score ≥ threshold |
| 🔄 OPPONENT | Rival team added/dropped a player | `rosters` diff between snapshots |
| 🏆 START/SIT | Weekly optimal lineup recommendation | `matchup_schedule` + `rankings` |
| 📊 RANK | Player moved ±10+ spots on FantasyPros ECR | `rankings` delta between builds |
| 🏈 PICK'EM | Weekly game pick with confidence % | `pickem` table + schedule lines |
| ⚠️ DEPTH | Player dropped to 2nd/3rd string | `players` depth_chart_position change |

---

## Message Templates

Each alert type has a strict, copyable format:

```
🚨 INJURY — {name} now {status}: {headline}
   League: {league_name} ({platform})  |  Pos: {position}  |  Team: {team}
   Action: {drop/stream/hold}

📈 WAIVER — {name} free in {league_name}
   Proj PPG: {projected_pts}  |  Priority: #{priority_score}
   Claim over: {player_to_drop}

🔄 OPPONENT — {opponent_team} added {name} in {league_name}
   Adjust waiver strategy: {reason}

🏆 START/SIT — Week {week} [{league_name}]
   Start {player_a} over {player_b}
   Edge: {matchup_note}

📊 RANK — {name} moved {old_rank}→{new_rank} on FantasyPros
   {context_note}

🏈 PICK'EM — Week {week}: {team_a} over {team_b}
   Conf: {confidence}%  |  Line: {spread}  |  Home: {home_team}

⚠️ DEPTH — {name} dropped to {depth_string} string
   Impact: {H/M/L}  |  League: {league_name}
```

---

## Architecture

```
PostgreSQL  ──►  hermes-worker (Python)  ──►  Telegram Bot API
     │                   │
     │           Event poller loop
     │           (reads player_events, waiver_targets,
     │            rosters diff, rankings delta, pickem)
     │                   │
     └── alert_sent flag  └── JSON structured log
         updated on                (stdout → Docker logs)
         successful send
```

### Service Contract

- **Container name**: `hermes`
- **Image**: `python:3.12-slim`
- **Networks**: `default` (DB access), no proxy_net needed
- **Secrets via env**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Poll interval**: `HERMES_POLL_INTERVAL_SECONDS` (default: 60)
- **Entry point**: `fantasy-dashboard/app/hermes/worker.py`

### Key env vars

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATABASE_URL=postgresql+psycopg://fantasy:${POSTGRES_PASSWORD}@db:5432/fantasy
HERMES_POLL_INTERVAL_SECONDS=60
HERMES_IMPACT_THRESHOLD=3          # min impact score to fire immediate alert
HERMES_DIGEST_HOUR=8               # hour (CDT) to send low-priority digest
HERMES_WAIVER_THRESHOLD=60         # min priority_score to alert a waiver target
HERMES_RANK_DELTA_THRESHOLD=10     # ± ECR spots to trigger rank alert
TZ=America/Chicago
```

---

## Database Integration

Hermes reads from and writes to existing schema — no new tables needed at v1.

| Table | Read | Write |
|---|---|---|
| `player_events` | Unread events (`alert_sent = false`) | Sets `alert_sent = true` after delivery |
| `waiver_targets` | `claim_recommendation` = true, not yet alerted | — |
| `rosters` | Latest two snapshots per league for diff | — |
| `rankings` | Delta between last two nightly builds | — |
| `pickem` | Current week picks | — |
| `players` | `depth_chart_position`, `status`, `team`, `position` | — |
| `leagues` | `league_name`, `platform`, `team_id` for scoping | — |

---

## File Layout

```
fantasy-dashboard/
  app/
    hermes/
      worker.py          # main poll loop + dispatcher
      formatters.py      # one function per alert type → str
      telegram.py        # send_message() with retry + backoff
      dedup.py           # mark_sent(), was_sent() helpers
      config.py          # env var loading + validation
  sql/
    002_hermes_alert_log.sql   # optional: persistent alert_log table for audit
```

---

## compose.yaml Service Block

```yaml
  hermes:
    image: python:3.12-slim
    container_name: fantasy-hermes
    working_dir: /app
    command: >
      bash -lc "pip install --no-cache-dir sqlalchemy 'psycopg[binary]' httpx &&
      python worker.py"
    environment:
      TZ: America/Chicago
      DATABASE_URL: postgresql+psycopg://fantasy:${POSTGRES_PASSWORD}@db:5432/fantasy
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
      HERMES_POLL_INTERVAL_SECONDS: ${HERMES_POLL_INTERVAL_SECONDS:-60}
      HERMES_IMPACT_THRESHOLD: ${HERMES_IMPACT_THRESHOLD:-3}
      HERMES_DIGEST_HOUR: ${HERMES_DIGEST_HOUR:-8}
      HERMES_WAIVER_THRESHOLD: ${HERMES_WAIVER_THRESHOLD:-60}
      HERMES_RANK_DELTA_THRESHOLD: ${HERMES_RANK_DELTA_THRESHOLD:-10}
    volumes:
      - ./app/hermes:/app
    depends_on:
      db:
        condition: service_healthy
    networks:
      - default
    restart: unless-stopped
```

---

## Build Phases

### Phase 1 — Skeleton (Day 1)
- [ ] `telegram.py` — `send_message()` with `httpx`, retry on 429, structured log
- [ ] `config.py` — load + validate all env vars, fail fast on missing secrets
- [ ] `worker.py` — bare poll loop: connect DB, sleep interval, log heartbeat
- [ ] Add `hermes` service to `compose.yaml`
- [ ] Smoke test: `docker compose up hermes` → heartbeat log every 60s

### Phase 2 — Injury + Depth Alerts (Day 2)
- [ ] `formatters.py` — `fmt_injury()`, `fmt_depth()`
- [ ] Poll `player_events` WHERE `alert_sent = false` AND `impact >= threshold`
- [ ] Mark `alert_sent = true` inside a transaction after successful send
- [ ] Test: insert a synthetic `player_events` row, verify Telegram fires once

### Phase 3 — Waiver + Rank Alerts (Day 3)
- [ ] `fmt_waiver()` — reads `waiver_targets` + joins `players`
- [ ] `fmt_rank()` — compares last two rows per player in `rankings`
- [ ] Deduplicate rank alerts: only fire once per rank build cycle

### Phase 4 — Opponent + Start/Sit + Pick'em (Day 4–5)
- [ ] `fmt_opponent()` — diff last two `rosters` snapshots per league
- [ ] `fmt_start_sit()` — reads `matchup_schedule` + top ranked available players
- [ ] `fmt_pickem()` — reads `pickem` for current week

### Phase 5 — Digest + Hardening
- [ ] Low-priority (impact 1–2) alerts batched into a single daily digest message
- [ ] `002_hermes_alert_log.sql` — persistent log of every fired alert (type, player_id, league_id, sent_at, message_text)
- [ ] Prometheus-style counters exposed on `/metrics` via FastAPI for monitoring

---

## Future Ideas

- **Multi-channel routing** — route INJURY to a separate urgent channel; RANK to a digest-only channel
- **User opt-in** — per-league Telegram chat IDs so league-mates can subscribe without seeing rival intel
- **LLM enrichment** — pass alert payload to local Ollama model to generate a one-sentence plain-English recommendation before dispatch
- **Discord fallback** — secondary webhook if Telegram is unreachable
- **Alert suppression window** — don't re-alert the same player within a configurable cooldown (e.g., 4h for injury updates)
- **Web UI feed** — POST every alert to the Node.js dashboard for an in-browser alert timeline alongside the draft board

---

## Success Criteria

- No alert fires more than once for the same event
- Injury alert latency < 2 minutes from `player_events` insert to Telegram delivery
- Zero uncaught exceptions crash the container — all errors logged and retried
- All seven alert types covered and tested with synthetic DB fixtures
- `docker compose logs hermes` shows structured JSON for every alert dispatched
