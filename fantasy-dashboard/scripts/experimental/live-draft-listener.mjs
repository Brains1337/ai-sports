import "dotenv/config";
import WebSocket from "ws";

const required = ["ESPN_S2", "SWID", "LEAGUE_ID", "SEASON"];
for (const key of required) {
  if (!process.env[key]) {
    throw new Error(`Missing ${key} in /opt/stacks/fantasy-dashboard/.env`);
  }
}

const {
  ESPN_S2,
  SWID,
  LEAGUE_ID,
  SEASON,
  ESPN_DRAFT_WS_URL,
} = process.env;

if (!ESPN_DRAFT_WS_URL) {
  throw new Error(
    "Missing ESPN_DRAFT_WS_URL in .env. Add a current draft WebSocket URL."
  );
}

const ws = new WebSocket(ESPN_DRAFT_WS_URL, {
  headers: {
    Origin: "https://fantasy.espn.com",
    Cookie: `espn_s2=${ESPN_S2}; SWID=${SWID}`,
    "User-Agent": "Mozilla/5.0",
  },
});

ws.on("open", () => {
  console.log(`Connected: league=${LEAGUE_ID}, season=${SEASON}`);
});

ws.on("message", (data) => {
  const message = data.toString();
  console.log(message);
});

ws.on("error", (err) => {
  console.error("WebSocket error:", err.message);
});

ws.on("close", (code, reason) => {
  console.log(`Closed: ${code} ${reason.toString()}`);
});
