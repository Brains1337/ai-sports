import { pollFantraxLeagues } from '../../app/lib/providers/fantrax/fantrax-poller.js';
import { makeFantraxState } from '../../app/lib/providers/fantrax/fantrax-state.js';

const RAW_LEAGUE_IDS =
  process.env.FANTRAX_LEAGUE_IDS ||
  process.env.FANTRAX_LEAGUE_ID ||
  '';

const LEAGUE_IDS = RAW_LEAGUE_IDS
  .split(',')
  .map(s => s.trim())
  .filter(Boolean);

const FANTRAX_COOKIE = process.env.FANTRAX_COOKIE || '';
const FANTRAX_POLL_MS = Number(process.env.FANTRAX_POLL_MS || 3000);
const FANTRAX_INGEST_URL =
  process.env.FANTRAX_INGEST_URL ||
  'http://dashboard:3000/provider/fantrax/draft-event';
const DRAFT_BRIDGE_TOKEN = process.env.DRAFT_BRIDGE_TOKEN || '';

if (!LEAGUE_IDS.length) {
  console.error('Missing FANTRAX_LEAGUE_IDS or FANTRAX_LEAGUE_ID');
  process.exit(1);
}

const state = makeFantraxState(LEAGUE_IDS);

async function postEvent(evt) {
  const res = await fetch(FANTRAX_INGEST_URL, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(DRAFT_BRIDGE_TOKEN ? { authorization: `Bearer ${DRAFT_BRIDGE_TOKEN}` } : {})
    },
    body: JSON.stringify({
      provider: 'fantrax',
      evt
    })
  });

  const text = await res.text();
  if (!res.ok) {
    throw new Error(`Ingest failed ${res.status}: ${text}`);
  }

  console.log(
    `[fantrax] forwarded league=${evt.leagueId} type=${evt.type} team=${evt.teamId || '-'} player=${evt.playerId || '-'} pick=${evt.overallPick || '-'}`
  );
}

async function tick() {
  await pollFantraxLeagues({
    leagueIds: LEAGUE_IDS,
    cookie: FANTRAX_COOKIE,
    state,
    onEvent: postEvent,
    logger: console
  });
}

console.log(`[fantrax] starting poller for ${LEAGUE_IDS.length} league(s): ${LEAGUE_IDS.join(', ')}`);
await tick();
setInterval(() => {
  tick().catch(err => console.error('[fantrax] tick failed', err));
}, FANTRAX_POLL_MS);
