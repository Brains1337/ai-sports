# AI Sports Platform

Fantasy sports intelligence platform built on a self-hosted AI infrastructure stack. Provides real-time draft assistance, rankings synthesis, and live scoring data across multiple fantasy providers (ESPN, Fantrax, FantasyPros).

## Architecture

```
ai-sports/
├── fantasy-dashboard/          # Core fantasy sports application
│   ├── app/
│   │   ├── api/                # Python FastAPI backend
│   │   │   ├── api/routes.py   # REST API endpoints
│   │   │   ├── database.py     # PostgreSQL connection layer
│   │   │   ├── main.py         # FastAPI app entrypoint
│   │   │   └── requirements.txt
│   │   └── dashboard/          # Node.js frontend server
│   │       ├── lib/providers/fantrax/   # Fantrax integration
│   │       ├── public/draft-dashboard.html
│   │       ├── server.js
│   │       ├── package.json
│   │       └── package-lock.json
│   ├── scripts/
│   │   ├── pollers/            # Live draft polling (Python + JS)
│   │   ├── experimental/       # ESPN bridge & live listeners
│   │   ├── build_rankings.py   # FantasyPros rankings builder
│   │   ├── sync_espn.py        # ESPN data sync
│   │   └── sync_fantasypros.py # FantasyPros data sync
│   ├── sql/init.sql            # Database schema
│   └── compose.yaml            # Docker Compose for fantasy-dashboard stack
├── dockge/                     # Dockge container manager
│   └── compose.yaml
├── npm/                        # NPM registry (Verdaccio)
│   └── compose.yaml
├── .gitattributes              # LF line ending enforcement
└── .gitignore
```

## Services

| Service | Description | Port |
|---------|-------------|------|
| FastAPI | Python REST API — rankings, player data, draft state | `8000` |
| Node.js Dashboard | Live draft dashboard served via Express | `3000` |
| PostgreSQL | Persistent storage for players, rankings, draft picks | `5432` |
| Dockge | Docker Compose stack manager UI | `5001` |
| Verdaccio | Self-hosted NPM registry | `4873` |

## Prerequisites

- Docker & Docker Compose v2
- Python 3.11+
- Node.js 20+
- PostgreSQL 15+ (or run via Docker)

## Getting Started

### 1. Clone the repository

```bash
git clone https://gitlab.com/ai-platform4863732/ai-sports.git
cd ai-sports
```

### 2. Configure environment

Copy the example env file and fill in your values:

```bash
cp fantasy-dashboard/.env.example fantasy-dashboard/.env
```

Required variables:

```env
POSTGRES_USER=aisports_admin
POSTGRES_PASSWORD=your_password
POSTGRES_DB=aisports
FANTRAX_LEAGUE_ID=your_league_id
ESPN_LEAGUE_ID=your_league_id
ESPN_S2=your_espn_s2_cookie
ESPN_SWID=your_espn_swid_cookie
```

### 3. Start the stack

```bash
cd fantasy-dashboard
docker compose up -d
```

### 4. Initialize the database

```bash
docker exec -i fantasy-dashboard-postgres-1 psql -U aisports_admin -d aisports < sql/init.sql
```

### 5. Run rankings sync

```bash
python scripts/sync_fantasypros.py
python scripts/build_rankings.py
```

## Development

### Python API (local)

```bash
cd fantasy-dashboard/app/api
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Node.js Dashboard (local)

```bash
cd fantasy-dashboard/app/dashboard
npm install --registry http://llm-sports-p01:4873
node server.js
```

### Live Draft Polling

```bash
# Fantrax draft poller
node scripts/pollers/fantrax-draft-poller.mjs

# Python quick poll
python scripts/pollers/quick_draft_poll.py
```

## Mirroring

This repository is **privately hosted on GitLab** and automatically mirrored to GitHub as a public read-only copy.

| Platform | URL |
|----------|-----|
| GitLab (source of truth) | https://gitlab.com/ai-platform4863732/ai-sports |
| GitHub (public mirror) | https://github.com/Brains1337/ai-sports |

All contributions and issues should be directed to the GitLab project.

## Contributing

1. Create a feature branch from `main`
2. Make changes and commit with clear messages
3. Open a Merge Request in GitLab
4. CI pipeline must pass before merge

## License

Private — All rights reserved.
