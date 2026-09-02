import express from 'express';
import path from 'path';
import { fileURLToPath } from 'url';
import { readFileSync } from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

const app  = express();
const PORT = process.env.PORT || 8787;
const AUTH_TOKEN = process.env.DRAFT_BRIDGE_TOKEN;

const ESPN_SEASON = process.env.ESPN_SEASON || process.env.SEASON || new Date().getFullYear();
const ESPN_LEAGUE = (process.env.ESPN_LEAGUE_IDS || '').split(',').map(s => s.trim()).filter(Boolean)[0] || process.env.LEAGUE_ID;
const ESPN_SWID   = process.env.ESPN_SWID;
const ESPN_S2     = process.env.ESPN_S2;

// ── Rankings CSV path ─────────────────────────────────────────────────────────
// Default: /opt/stacks/fantasy-dashboard/data/board.csv  (matches your server)
// Override with RANKINGS_CSV env var if needed
const RANKINGS_CSV = process.env.RANKINGS_CSV || '/data/board.csv';

function parseRankingsCSV(filepath) {
  try {
    const lines = readFileSync(filepath, 'utf8').trim().split('\n');
    const headers = lines[0].split(',').map(h => h.trim());
    return lines.slice(1).map(line => {
      // Handle names with commas (quoted fields) — simple split is fine for this CSV
      const vals = line.split(',');
      const row  = {};
      headers.forEach((h, i) => { row[h] = (vals[i] || '').trim(); });
      return {
        rank:    parseInt(row.overall, 10)  || 9999,
        name:    row.name                   || '',
        pos:     row.pos                    || '',
        posRank: parseInt(row.pos_rank, 10) || 0,
        tier:    parseInt(row.tier, 10)     || 0,
        team:    row.team                   || '',
        bye:     parseInt(row.bye, 10)      || 0,
        pts:     parseFloat(row.pts)        || 0,
        ppg:     parseFloat(row.ppg)        || 0,
        vorp:    parseFloat(row.vorp)       || 0,
        adp:     parseFloat(row.adp)        || 9999,
        injury:  row.injury                 || 'ACTIVE',
      };
    }).filter(p => p.name);
  } catch (e) {
    console.error('[rankings] CSV parse error:', e.message);
    return [];
  }
}

app.use(express.json({ limit: '1mb' }));
app.use(express.static(path.join(__dirname, 'public')));

// ── Per-league rooms ──────────────────────────────────────────────────────────

const rooms = new Map();   // leagueId → RoomState

function makeRoom(leagueId) {
  return {
    leagueId,
    provider: 'espn',
    draftUrl: null,
    lastUpdatedAt: null,
    currentPick: null,
    clockMs: null,
    onTheClockTeamId: null,
    recentEvents: [],
    recentPicks: [],
    playerCache: {},
    teams: {},
    rawFramesSeen: 0,
    uniqueFramesSeen: 0,
    lastFrameText: null,
    startedAt: new Date().toISOString(),
    clients: new Set(),
    dedupe: new Map()
  };
}

function getRoom(leagueId) {
  const key = String(leagueId || '__default__');
  if (!rooms.has(key)) rooms.set(key, makeRoom(key));
  return rooms.get(key);
}

function roomPublicState(room) {
  const { clients: _c, dedupe: _d, ...pub } = room;
  return pub;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function setBridgeCors(res) {
  res.set({
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization'
  });
}

function sendEvent(res, event, data) {
  res.write(`event: ${event}\n`);
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

function broadcast(room, event, data) {
  for (const res of room.clients) sendEvent(res, event, data);
}

function rememberEvent(room, evt) {
  room.recentEvents.unshift(evt);
  room.recentEvents = room.recentEvents.slice(0, 40);
}

function rememberPick(room, pick) {
  room.recentPicks.unshift(pick);
  room.recentPicks = room.recentPicks.slice(0, 24);
}

function rememberCachedPlayer(room, playerId, extra = {}) {
  if (!playerId) return;
  const id = String(playerId);
  if (!room.playerCache[id]) {
    room.playerCache[id] = {
      id,
      fullName: extra.fullName || null,
      pos: extra.pos || null,
      proTeam: extra.proTeam || null
    };
  } else {
    if (extra.fullName && !room.playerCache[id].fullName)
      room.playerCache[id].fullName = extra.fullName;
    if (extra.pos && !room.playerCache[id].pos)
      room.playerCache[id].pos = extra.pos;
    if (extra.proTeam && !room.playerCache[id].proTeam)
      room.playerCache[id].proTeam = extra.proTeam;
  }
}

function ensureTeam(room, teamId, extra = {}) {
  if (!teamId) return null;
  if (!room.teams[teamId]) {
    room.teams[teamId] = {
      teamId,
      name: extra.name || `Team ${teamId}`,
      picks: [],
      lastPickAt: null
    };
  } else if (extra.name && room.teams[teamId].name === `Team ${teamId}`) {
    room.teams[teamId].name = extra.name;
  }
  return room.teams[teamId];
}

function parseLeagueId(url) {
  const v = String(url || '');
  const espn = v.match(/league[_-]?(\d{6,})/i);
  if (espn?.[1]) return espn[1];
  const fantrax = v.match(/[?&]leagueId=([a-zA-Z0-9]+)/);
  if (fantrax?.[1]) return fantrax[1];
  return null;
}

function pruneDedupe(room) {
  const now = Date.now();
  for (const [key, ts] of room.dedupe) {
    if (now - ts > 30000) room.dedupe.delete(key);
  }
}

function normalizeFrame(text) {
  const line = String(text || '').trim();
  if (!line) return null;
  const parts = line.split(/\s+/);
  const type  = parts[0];
  if (type === 'CLOCK')       return { type: 'CLOCK', raw: line };
  if (type === 'SELECTING')   return { type: 'SELECTING',  teamId: parts[1]||null, clockMs: Number(parts[2])||null, raw: line };
  if (type === 'SELECTED')    return { type: 'SELECTED',   teamId: parts[1]||null, playerId: parts[2]||null, overallPick: Number(parts[3])||null, raw: line };
  if (type === 'AUTOSUGGEST') return { type: 'AUTOSUGGEST', playerId: parts[1]||null, raw: line };
  return { type: 'RAW', raw: line };
}

function makePublicEvent({ receivedAt, source, frame, via=null, url=null, page=null, raw=null }) {
  return { receivedAt: receivedAt||new Date().toISOString(), source: source||'bridge', frame: frame||{ type:'RAW',raw:null }, via, url, page, raw };
}

function requireBridgeAuth(req, res) {
  const auth = req.get('authorization') || '';
  if (!AUTH_TOKEN)                     { res.status(500).json({ ok:false, error:'missing_bridge_token'  }); return false; }
  if (auth !== `Bearer ${AUTH_TOKEN}`) { res.status(401).json({ ok:false, error:'unauthorized'          }); return false; }
  return true;
}

// ── ESPN helpers ──────────────────────────────────────────────────────────────

function espnHeaders() {
  return {
    Accept: 'application/json,text/plain,*/*',
    'User-Agent': 'fantasy-dashboard/0.1',
    Cookie: `SWID=${ESPN_SWID}; espn_s2=${ESPN_S2}`
  };
}

function espnLeagueBase(leagueId) {
  const lid = leagueId || ESPN_LEAGUE;
  return `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/${ESPN_SEASON}/segments/0/leagues/${lid}`;
}

function normalizeEspnPlayer(item) {
  const poolEntry = item?.playerPoolEntry || {};
  const player = item?.player || poolEntry.player || poolEntry;
  const id = player?.id ?? item?.id ?? item?.playerId ?? poolEntry?.id;
  if (id == null) return null;

  const ownership = player?.ownership || item?.ownership || poolEntry?.ownership || {};
  const pprRank = player?.draftRanksByRankType?.PPR?.rank;
  const standardRank = player?.draftRanksByRankType?.STANDARD?.rank;
  const onTeamId = item?.onTeamId ?? poolEntry?.onTeamId ?? null;
  const fullName =
    player?.fullName ||
    player?.displayName ||
    player?.name ||
    poolEntry?.playerName ||
    item?.fullName ||
    item?.playerName ||
    null;

  return {
    id:       String(id),
    fullName,
    pos:      player?.defaultPositionId ?? poolEntry?.defaultPositionId ?? null,
    proTeam:  player?.proTeamId ?? poolEntry?.proTeamId ?? null,
    rank:     player?.rankCalculatedFinal ?? item?.rankCalculatedFinal ?? pprRank ?? standardRank ?? 9999,
    adp:      ownership?.averageDraftPosition ?? player?.averageDraftPosition ?? item?.averageDraftPosition ?? 9999,
    drafted:  Number(onTeamId || 0) > 0,
    onTeamId: Number(onTeamId || 0) > 0 ? String(onTeamId) : null
  };
}

// ── Event application ─────────────────────────────────────────────────────────

function applyEspnEvent(body, room) {
  const payload = body.payload || {};
  const text    = String(payload.text || '');
  const frame   = normalizeFrame(text);
  const lid     = parseLeagueId(payload.url || body.url || body.page);

  room.provider      = 'espn';
  room.rawFramesSeen += 1;
  room.lastUpdatedAt = new Date().toISOString();
  room.lastFrameText = text.trim() || null;

  if (lid && room.leagueId === '__default__') room.leagueId = lid;
  if (body.page) room.draftUrl = body.page;

  const fingerprint = `espn|${payload.url||''}|${text.trim()}|${payload.phase||''}`;
  const now = Date.now();
  const seenAt = room.dedupe.get(fingerprint);
  if (seenAt && now - seenAt < 1000) return { ignored:true, reason:'duplicate', frame };
  room.dedupe.set(fingerprint, now);
  pruneDedupe(room);
  room.uniqueFramesSeen += 1;

  const event = makePublicEvent({ receivedAt:body.receivedAt, source:body.from||'bridge', frame, via:payload.via||null, url:payload.url||body.url||null, page:body.page||null, raw:body });
  rememberEvent(room, event);

  if (!frame) { broadcast(room,'draft:event',event); broadcast(room,'draft:state',roomPublicState(room)); return { ignored:false,event }; }

  if (frame.type==='CLOCK')      { broadcast(room,'draft:clock',event); }
  if (frame.type==='SELECTING')  { room.onTheClockTeamId=frame.teamId; room.clockMs=frame.clockMs; room.currentPick=room.currentPick||1; ensureTeam(room,frame.teamId); broadcast(room,'draft:selecting',event); }
  if (frame.type==='SELECTED')   {
    rememberCachedPlayer(room, frame.playerId);
    const team = ensureTeam(room, frame.teamId);
    const cached = room.playerCache[String(frame.playerId)] || {};
    const pick = { provider:'espn', teamId:frame.teamId, playerId:frame.playerId, playerName:cached.fullName||null, pos:cached.pos||null, overallPick:frame.overallPick, round:null, pickInRound:null, at:event.receivedAt };
    if (frame.overallPick) room.currentPick = frame.overallPick + 1;
    if (team) { team.picks.unshift(pick); team.picks=team.picks.slice(0,30); team.lastPickAt=event.receivedAt; }
    rememberPick(room, pick);
    broadcast(room,'draft:selected',{ ...event, pick });
  }
  if (frame.type==='AUTOSUGGEST') { broadcast(room,'draft:autosuggest',event); }
  if (frame.type==='RAW')         { broadcast(room,'draft:raw',event); }

  broadcast(room,'draft:state',roomPublicState(room));
  return { ignored:false, event };
}

function applyProviderEvent(evt, room) {
  const provider = evt.provider || 'unknown';
  const type     = evt.type     || 'RAW';

  room.provider      = provider;
  room.lastUpdatedAt = new Date().toISOString();
  if (evt.leagueId)                room.leagueId           = evt.leagueId;
  if (evt.draftUrl)                room.draftUrl            = evt.draftUrl;
  if (evt.clockMs       != null)   room.clockMs             = evt.clockMs;
  if (evt.onTheClockTeamId != null) room.onTheClockTeamId  = evt.onTheClockTeamId;

  const fingerprint = [provider, evt.leagueId||'', type, evt.teamId||'', evt.playerId||'', evt.overallPick||'', evt.round||'', evt.pickInRound||'', evt.receivedAt||''].join('|');
  const now = Date.now();
  const seenAt = room.dedupe.get(fingerprint);
  if (seenAt && now - seenAt < 1000) return { ignored:true, reason:'duplicate', evt };
  room.dedupe.set(fingerprint, now);
  pruneDedupe(room);
  room.rawFramesSeen    += 1;
  room.uniqueFramesSeen += 1;

  if (type === 'LEAGUE_META') {
    for (const t of evt.teams||[]) ensureTeam(room, t.teamId, { name:t.name });
    const event = makePublicEvent({ receivedAt:evt.receivedAt, source:provider, frame:{ type:'LEAGUE_META',raw:provider }, via:provider, raw:evt });
    rememberEvent(room, event);
    broadcast(room,'draft:event',event);
    broadcast(room,'draft:state',roomPublicState(room));
    return { ignored:false, event };
  }

  if (type === 'CLOCK') {
    const event = makePublicEvent({ receivedAt:evt.receivedAt, source:provider, frame:{ type:'CLOCK',raw:evt.raw||provider }, via:provider, raw:evt });
    rememberEvent(room, event);
    broadcast(room,'draft:clock',event);
    broadcast(room,'draft:state',roomPublicState(room));
    return { ignored:false, event };
  }

  if (type === 'SELECTING') {
    room.onTheClockTeamId = evt.teamId||null;
    room.clockMs          = evt.clockMs??room.clockMs;
    room.currentPick      = evt.overallPick||room.currentPick||1;
    ensureTeam(room, evt.teamId);
    const event = makePublicEvent({ receivedAt:evt.receivedAt, source:provider, frame:{ type:'SELECTING', teamId:evt.teamId||null, clockMs:evt.clockMs??null, raw:evt.raw||provider }, via:provider, raw:evt });
    rememberEvent(room, event);
    broadcast(room,'draft:selecting',event);
    broadcast(room,'draft:state',roomPublicState(room));
    return { ignored:false, event };
  }

  if (type === 'SELECTED') {
    rememberCachedPlayer(room, evt.playerId, {
      fullName: evt.playerName || evt.fullName,
      pos: evt.pos,
      proTeam: evt.proTeam
    });
    const team = ensureTeam(room, evt.teamId);
    const cached = room.playerCache[String(evt.playerId)] || {};
    const pick = { provider, teamId:evt.teamId||null, playerId:evt.playerId||null, playerName:evt.playerName||evt.fullName||cached.fullName||null, pos:evt.pos||cached.pos||null, overallPick:evt.overallPick??null, round:evt.round??null, pickInRound:evt.pickInRound??null, at:evt.receivedAt||new Date().toISOString() };
    if (evt.overallPick) room.currentPick = evt.overallPick + 1;
    else if (!room.currentPick) room.currentPick = 1;
    if (team) { team.picks.unshift(pick); team.picks=team.picks.slice(0,30); team.lastPickAt=pick.at; }
    rememberPick(room, pick);
    const event = makePublicEvent({ receivedAt:pick.at, source:provider, frame:{ type:'SELECTED', teamId:evt.teamId||null, playerId:evt.playerId||null, overallPick:evt.overallPick??null, raw:evt.raw||provider }, via:provider, raw:evt });
    rememberEvent(room, event);
    broadcast(room,'draft:selected',{ ...event, pick });
    broadcast(room,'draft:state',roomPublicState(room));
    return { ignored:false, event };
  }

  const event = makePublicEvent({ receivedAt:evt.receivedAt, source:provider, frame:{ type, raw:evt.raw||provider }, via:provider, raw:evt });
  rememberEvent(room, event);
  if (type === 'AUTOSUGGEST') broadcast(room,'draft:autosuggest',event);
  else                        broadcast(room,'draft:event',event);
  broadcast(room,'draft:state',roomPublicState(room));
  return { ignored:false, event };
}

// ── Routes ────────────────────────────────────────────────────────────────────

// List active rooms
app.get('/api/rooms', (_req, res) => {
  const list = [...rooms.values()].map(r => ({
    leagueId:      r.leagueId,
    provider:      r.provider,
    draftUrl:      r.draftUrl,
    lastUpdatedAt: r.lastUpdatedAt,
    currentPick:   r.currentPick,
    teamCount:     Object.keys(r.teams).length,
    pickCount:     r.recentPicks.length,
    clientCount:   r.clients.size,
    startedAt:     r.startedAt
  }));
  res.json({ count: list.length, rooms: list });
});

// Single room state
app.get('/api/state', (req, res) => {
  const room = getRoom(req.query.leagueId);
  res.json(roomPublicState(room));
});

// SSE stream — per-room
app.get('/api/stream', (req, res) => {
  const room = getRoom(req.query.leagueId);
  res.writeHead(200, {
    'Content-Type':  'text/event-stream',
    'Cache-Control': 'no-cache, no-transform',
    'Connection':    'keep-alive',
    'X-Accel-Buffering': 'no'
  });
  if (typeof res.flushHeaders === 'function') res.flushHeaders();
  res.write(': connected\n\n');
  room.clients.add(res);
  sendEvent(res, 'draft:state', roomPublicState(room));
  const heartbeat = setInterval(() => res.write(': heartbeat\n\n'), 15000);
  req.on('close', () => { clearInterval(heartbeat); room.clients.delete(res); });
});

// ── Rankings endpoint — reads board.csv fresh on every request ────────────────
app.get('/api/rankings', (_req, res) => {
  const items = parseRankingsCSV(RANKINGS_CSV);
  res.json({ count: items.length, items });
});

// ESPN bridge
app.options('/espn/draft-event', (_req, res) => { setBridgeCors(res); res.sendStatus(204); });
app.post('/espn/draft-event', (req, res) => {
  setBridgeCors(res);
  if (!requireBridgeAuth(req, res)) return;
  const body     = req.body || {};
  const evt      = body.evt || body;
  const leagueId = req.query.leagueId || parseLeagueId((evt.payload||{}).url || evt.url || evt.page) || '__default__';
  const room     = getRoom(leagueId);
  const result   = applyEspnEvent(evt, room);
  res.json({ ok:true, result });
});

// Provider bridge
app.options('/provider/:provider/draft-event', (_req, res) => { setBridgeCors(res); res.sendStatus(204); });
app.post('/provider/:provider/draft-event', (req, res) => {
  setBridgeCors(res);
  if (!requireBridgeAuth(req, res)) return;
  const body     = req.body || {};
  const evt      = body.evt || body;
  const provider = req.params.provider || evt.provider || 'unknown';
  const leagueId = req.query.leagueId || evt.leagueId || '__default__';
  const room     = getRoom(leagueId);
  const result   = applyProviderEvent({ ...evt, provider }, room);
  res.json({ ok:true, result });
});

// ESPN data endpoints
// Player cache built from live draft pick events
app.get('/api/players', (req, res) => {
  const leagueId = req.query.leagueId;
  const room = leagueId ? getRoom(leagueId) : null;
  const items = room ? Object.values(room.playerCache) : [];
  res.json({ count: items.length, items });
});

app.get('/api/espn/players', async (req, res) => {
  const leagueId = req.query.leagueId || ESPN_LEAGUE;
  if (!leagueId) return res.status(400).json({ error:'leagueId required' });
  try {
    const url = `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/${ESPN_SEASON}/players?view=players_wl`;
    const fantasyFilter = {
      players: {
        limit: 2000,
        filterActive: { value: true }
      }
    };
    const resp = await fetch(url, {
      headers: {
        ...espnHeaders(),
        'X-Fantasy-Filter': JSON.stringify(fantasyFilter)
      }
    });
    if (!resp.ok) return res.status(resp.status).json({ error:`ESPN ${resp.status}` });
    const data = await resp.json();
    const items = Array.isArray(data) ? data : (data.players || []);
    const playersById = new Map();
    for (const item of items) {
      const player = normalizeEspnPlayer(item);
      if (!player) continue;
      const current = playersById.get(player.id);
      playersById.set(player.id, current ? {
        ...current,
        ...player,
        fullName: player.fullName || current.fullName,
        pos: player.pos ?? current.pos,
        proTeam: player.proTeam ?? current.proTeam,
        rank: player.rank < 9999 ? player.rank : current.rank,
        adp: player.adp < 9999 ? player.adp : current.adp,
        drafted: current.drafted || player.drafted,
        onTeamId: player.onTeamId || current.onTeamId
      } : player);
    }
    const players = [...playersById.values()];
    const room = getRoom(leagueId);
    for (const player of players) {
      rememberCachedPlayer(room, player.id, player);
    }
    players.sort((a,b) => a.rank - b.rank);
    res.json({ count:players.length, items:players });
  } catch (e) { res.status(500).json({ error:e.message }); }
});

app.get('/api/espn/teams', async (req, res) => {
  const leagueId = req.query.leagueId || ESPN_LEAGUE;
  if (!leagueId) return res.status(400).json({ error:'leagueId required' });
  try {
    const url  = `${espnLeagueBase(leagueId)}?view=mTeam`;
    const resp = await fetch(url, { headers: espnHeaders() });
    if (!resp.ok) return res.status(resp.status).json({ error:`ESPN ${resp.status}` });
    const data  = await resp.json();
    const teams = (data.teams||[]).reduce((acc,t) => {
      acc[String(t.id)] = [t.location,t.nickname].filter(Boolean).join(' ').trim() || t.name || t.abbrev || `Team ${t.id}`;
      return acc;
    }, {});
    res.json(teams);
  } catch (e) { res.status(500).json({ error:e.message }); }
});

app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'draft-dashboard.html'));
});

app.listen(PORT, () => {
  console.log(`Live draft dashboard listening on http://localhost:${PORT}`);
});
