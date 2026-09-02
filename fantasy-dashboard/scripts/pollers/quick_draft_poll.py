import os, time, requests

LEAGUE_ID = os.environ["LEAGUE_ID"]
SEASON = os.environ.get("SEASON", "2026")
SWID = os.environ["SWID"]
ESPNS2 = os.environ["ESPNS2"]

url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}/segments/0/leagues/{LEAGUE_ID}"
params = {"view": ["mDraftDetail", "mTeam"]}
cookies = {"SWID": SWID, "espn_s2": ESPNS2}

seen = {}

while True:
    r = requests.get(url, params=params, cookies=cookies, timeout=15)
    r.raise_for_status()
    j = r.json()
    picks = j["draftDetail"]["picks"]
    teams = {t["id"]: t.get("name", f"Team {t['id']}") for t in j.get("teams", [])}

    for p in picks:
        pid = p.get("playerId", -1)
        pick_id = p["id"]
        if pid != -1 and seen.get(pick_id) != pid:
            seen[pick_id] = pid
            print(
                f"NEW PICK overall={p['overallPickNumber']} round={p['roundId']}.{p['roundPickNumber']} "
                f"team={p['teamId']} teamName={teams.get(p['teamId'], p['teamId'])} playerId={pid}",
                flush=True
            )
    time.sleep(2)
