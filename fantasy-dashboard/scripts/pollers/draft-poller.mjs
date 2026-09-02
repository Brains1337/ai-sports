import "dotenv/config";
import { writeFile } from "node:fs/promises";

const required = ["ESPN_S2", "SWID", "LEAGUE_ID", "SEASON"];

for (const key of required) {
  if (!process.env[key]) {
    throw new Error(`Missing ${key} in .env`);
  }
}

const { ESPN_S2, SWID, LEAGUE_ID, SEASON } = process.env;

const url =
  `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/` +
  `seasons/${SEASON}/segments/0/leagues/${LEAGUE_ID}` +
  `?view=mDraftDetail&view=mSettings&view=mTeam`;

let lastPickCount = null;

async function poll() {
  try {
    const response = await fetch(url, {
      headers: {
        Cookie: `SWID=${SWID}; espn_s2=${ESPN_S2}`,
        Origin: "https://fantasy.espn.com",
        Referer: "https://fantasy.espn.com/",
        "User-Agent": "Mozilla/5.0",
      },
    });

    if (!response.ok) {
      throw new Error(`ESPN API returned HTTP ${response.status}`);
    }

    const league = await response.json();
    const draft = league.draftDetail ?? {};
    const picks = draft.picks ?? [];
    const made = picks.filter((pick) => Number(pick.playerId) > 0);
    const newest = made.at(-1);

    const state = {
      fetchedAt: new Date().toISOString(),
      leagueId: LEAGUE_ID,
      season: Number(SEASON),
      draftInProgress: Boolean(draft.inProgress),
      draftComplete: Boolean(draft.drafted),
      picksMade: made.length,
      nextOverallPick: made.length + 1,
      latestPick: newest ?? null,
      picks: made,
      teams: league.teams ?? [],
    };

    await writeFile(
      "/opt/stacks/fantasy-dashboard/draft-state.json",
      JSON.stringify(state, null, 2) + "\n"
    );

    if (lastPickCount !== made.length) {
      console.log(
        `[${state.fetchedAt}] ${made.length} picks made; ` +
        `next pick: ${state.nextOverallPick}`
      );
      if (newest) console.log("Latest:", newest);
      lastPickCount = made.length;
    }
  } catch (error) {
    console.error(`[${new Date().toISOString()}] ${error.message}`);
  }
}

await poll();
setInterval(poll, 5000);
