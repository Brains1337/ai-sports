import express from 'express';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = process.env.PORT || 8787;
const AUTH_TOKEN = process.env.DRAFT_BRIDGE_TOKEN;

app.use(express.json({ limit: '1mb' }));
app.use(express.static(path.join(__dirname, 'public')));

const clients = new Set();

const state = {
  leagueId: null,
  provider: 'espn',
  draftUrl: null,
  lastUpdatedAt: null,
  currentPick: null,
  clockMs: null,
  onTheClockTeamId: null,
  recentEvents: [],
  recentPicks: [],
  teams: {},
  rawFramesSeen: 0,
  uniqueFramesSeen: 0,
  lastFrameText: null,
  startedAt: new Date().toISOString()
};

const dedupe = new Map();

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

function broadcast(event, data) {
  for (const res of clients) sendEvent(res, event, data);
}

function rememberEvent(evt) {
  state.recentEvents.unshift(evt);
  state.recentEvents = state.recentEvents.slice(0, 40);
}

function rememberPick(pick) {
  state.recentPicks.unshift(pick);
  state.recentPicks = state.recentPicks.slice(0, 24);
}

function ensureTeam(teamId, extra = {}) {
  if (!teamId) return null;
  if (!state.teams[teamId]) {
    state.teams[teamId] = {
      teamId,
      name: extra.name || `Team ${teamId}`,
      picks: [],
      lastPickAt: null
    };
  } else if (extra.name && !state.teams[teamId].name) {
    state.teams[teamId].name = extra.name;
  }
  return state.teams[teamId];
}

function parseLeagueId(url) {
  const value = String(url || '');
  const espnMatch = value.match(/league-(\d+)/);
  if (espnMatch?.[1]) return espnMatch[1];

  const fantraxMatch = value.match(/[?&]leagueId=([a-zA-Z0-9]+)/);
  if (fantraxMatch?.[1]) return fantraxMatch[1];

  return null;
}

function normalizeFrame(text) {
  const line = String(text || '').trim();
  if (!line) return null;

  const parts = line.split(/\s+/);
  const type = parts[0];

  if (type === 'CLOCK') {
    return { type: 'CLOCK', raw: line };
  }

  if (type === 'SELECTING') {
    return {
      type: 'SELECTING',
      teamId: parts[1] || null,
      clockMs: Number(parts[2]) || null,
      raw: line
    };
  }

  if (type === 'SELECTED') {
    return {
      type: 'SELECTED',
      teamId: parts[1] || null,
      playerId: parts[2] || null,
      overallPick: Number(parts[3]) || null,
      raw: line
    };
  }

  if (type === 'AUTOSUGGEST') {
    return {
      type: 'AUTOSUGGEST',
      playerId: parts[1] || null,
      raw: line
    };
  }

  return { type: 'RAW', raw: line };
}

function makePublicEvent({
  receivedAt,
  source,
  frame,
  via = null,
  url = null,
  page = null,
  raw = null
}) {
  return {
    receivedAt: receivedAt || new Date().toISOString(),
    source: source || 'bridge',
    frame: frame || { type: 'RAW', raw: null },
    via,
    url,
    page,
    raw
  };
}

function pruneDedupe() {
  const now = Date.now();
  for (const [key, ts] of dedupe) {
    if (now - ts > 30000) dedupe.delete(key);
  }
}

function applyEspnEvent(body) {
  const payload = body.payload || {};
  const text = String(payload.text || '');
  const frame = normalizeFrame(text);
  const leagueId = parseLeagueId(payload.url || body.url || body.page);

  state.provider = 'espn';
  state.rawFramesSeen += 1;
  state.lastUpdatedAt = new Date().toISOString();
  state.lastFrameText = text.trim() || null;

  if (leagueId) state.leagueId = leagueId;
  if (body.page) state.draftUrl = body.page;

  const fingerprint = `espn|${payload.url || ''}|${text.trim()}|${payload.phase || ''}`;
  const now = Date.now();
  const seenAt = dedupe.get(fingerprint);
  if (seenAt && now - seenAt < 1000) {
    return { ignored: true, reason: 'duplicate', frame };
  }

  dedupe.set(fingerprint, now);
  pruneDedupe();
  state.uniqueFramesSeen += 1;

  const event = makePublicEvent({
    receivedAt: body.receivedAt,
    source: body.from || 'bridge',
    frame,
    via: payload.via || null,
    url: payload.url || body.url || null,
    page: body.page || null,
    raw: body
  });

  rememberEvent(event);

  if (!frame) {
    broadcast('draft:event', event);
    broadcast('draft:state', state);
    return { ignored: false, event };
  }

  if (frame.type === 'CLOCK') {
    broadcast('draft:clock', event);
  }

  if (frame.type === 'SELECTING') {
    state.onTheClockTeamId = frame.teamId;
    state.clockMs = frame.clockMs;
    state.currentPick = state.currentPick || 1;
    ensureTeam(frame.teamId);
    broadcast('draft:selecting', event);
  }

  if (frame.type === 'SELECTED') {
    const team = ensureTeam(frame.teamId);
    const pick = {
      provider: 'espn',
      teamId: frame.teamId,
      playerId: frame.playerId,
      overallPick: frame.overallPick,
      round: null,
      pickInRound: null,
      at: event.receivedAt
    };

    state.currentPick = frame.overallPick ? frame.overallPick + 1 : state.currentPick;

    if (team) {
      team.picks.unshift(pick);
      team.picks = team.picks.slice(0, 30);
      team.lastPickAt = event.receivedAt;
    }

    rememberPick(pick);
    broadcast('draft:selected', { ...event, pick });
  }

  if (frame.type === 'AUTOSUGGEST') {
    broadcast('draft:autosuggest', event);
  }

  if (frame.type === 'RAW') {
    broadcast('draft:raw', event);
  }

  broadcast('draft:state', state);
  return { ignored: false, event };
}

function applyProviderEvent(evt) {
  const provider = evt.provider || 'unknown';
  const type = evt.type || 'RAW';

  state.provider = provider;
  state.lastUpdatedAt = new Date().toISOString();

  if (evt.leagueId) state.leagueId = evt.leagueId;
  if (evt.draftUrl) state.draftUrl = evt.draftUrl;
  if (evt.clockMs !== undefined && evt.clockMs !== null) state.clockMs = evt.clockMs;
  if (evt.onTheClockTeamId !== undefined) state.onTheClockTeamId = evt.onTheClockTeamId;

  const fingerprint = [
    provider,
    evt.leagueId || '',
    type,
    evt.teamId || '',
    evt.playerId || '',
    evt.overallPick || '',
    evt.round || '',
    evt.pickInRound || '',
    evt.receivedAt || ''
  ].join('|');

  const now = Date.now();
  const seenAt = dedupe.get(fingerprint);
  if (seenAt && now - seenAt < 1000) {
    return { ignored: true, reason: 'duplicate', evt };
  }

  dedupe.set(fingerprint, now);
  pruneDedupe();

  state.rawFramesSeen += 1;
  state.uniqueFramesSeen += 1;
  state.lastFrameText = JSON.stringify({
    provider,
    type,
    teamId: evt.teamId || null,
    playerId: evt.playerId || null,
    overallPick: evt.overallPick || null
  });

  if (type === 'LEAGUE_META') {
    for (const team of evt.teams || []) {
      ensureTeam(team.teamId, { name: team.name });
    }

    const event = makePublicEvent({
      receivedAt: evt.receivedAt,
      source: provider,
      frame: { type: 'LEAGUE_META', raw: provider },
      via: provider,
      raw: evt
    });

    rememberEvent(event);
    broadcast('draft:event', event);
    broadcast('draft:state', state);
    return { ignored: false, event };
  }

  if (type === 'CLOCK') {
    const event = makePublicEvent({
      receivedAt: evt.receivedAt,
      source: provider,
      frame: { type: 'CLOCK', raw: evt.raw || provider },
      via: provider,
      raw: evt
    });

    rememberEvent(event);
    broadcast('draft:clock', event);
    broadcast('draft:state', state);
    return { ignored: false, event };
  }

  if (type === 'SELECTING') {
    state.onTheClockTeamId = evt.teamId || null;
    state.clockMs = evt.clockMs ?? state.clockMs;
    state.currentPick = evt.overallPick || state.currentPick || 1;
    ensureTeam(evt.teamId);

    const event = makePublicEvent({
      receivedAt: evt.receivedAt,
      source: provider,
      frame: {
        type: 'SELECTING',
        teamId: evt.teamId || null,
        clockMs: evt.clockMs ?? null,
        raw: evt.raw || provider
      },
      via: provider,
      raw: evt
    });

    rememberEvent(event);
    broadcast('draft:selecting', event);
    broadcast('draft:state', state);
    return { ignored: false, event };
  }

  if (type === 'SELECTED') {
    const team = ensureTeam(evt.teamId);
    const pick = {
      provider,
      teamId: evt.teamId || null,
      playerId: evt.playerId || null,
      overallPick: evt.overallPick ?? null,
      round: evt.round ?? null,
      pickInRound: evt.pickInRound ?? null,
      at: evt.receivedAt || new Date().toISOString()
    };

    if (evt.overallPick) {
      state.currentPick = evt.overallPick + 1;
    } else if (!state.currentPick) {
      state.currentPick = 1;
    }

    if (team) {
      team.picks.unshift(pick);
      team.picks = team.picks.slice(0, 30);
      team.lastPickAt = pick.at;
    }

    rememberPick(pick);

    const event = makePublicEvent({
      receivedAt: pick.at,
      source: provider,
      frame: {
        type: 'SELECTED',
        teamId: evt.teamId || null,
        playerId: evt.playerId || null,
        overallPick: evt.overallPick ?? null,
        raw: evt.raw || provider
      },
      via: provider,
      raw: evt
    });

    rememberEvent(event);
    broadcast('draft:selected', { ...event, pick });
    broadcast('draft:state', state);
    return { ignored: false, event };
  }

  const event = makePublicEvent({
    receivedAt: evt.receivedAt,
    source: provider,
    frame: {
      type,
      raw: evt.raw || provider
    },
    via: provider,
    raw: evt
  });

  rememberEvent(event);

  if (type === 'AUTOSUGGEST') {
    broadcast('draft:autosuggest', event);
  } else {
    broadcast('draft:event', event);
  }

  broadcast('draft:state', state);
  return { ignored: false, event };
}

function requireBridgeAuth(req, res) {
  const auth = req.get('authorization') || '';

  if (!AUTH_TOKEN) {
    res.status(500).json({ ok: false, error: 'missing_bridge_token' });
    return false;
  }

  if (auth !== `Bearer ${AUTH_TOKEN}`) {
    res.status(401).json({ ok: false, error: 'unauthorized' });
    return false;
  }

  return true;
}

app.get('/api/state', (_req, res) => {
  res.json(state);
});

app.get('/api/stream', (req, res) => {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no'
  });

  if (typeof res.flushHeaders === 'function') {
    res.flushHeaders();
  }

  res.write(': connected\n\n');
  clients.add(res);
  sendEvent(res, 'draft:state', state);

  const heartbeat = setInterval(() => {
    res.write(': heartbeat\n\n');
  }, 15000);

  req.on('close', () => {
    clearInterval(heartbeat);
    clients.delete(res);
  });
});

app.options('/espn/draft-event', (_req, res) => {
  setBridgeCors(res);
  return res.sendStatus(204);
});

app.post('/espn/draft-event', (req, res) => {
  setBridgeCors(res);

  if (!requireBridgeAuth(req, res)) return;

  const body = req.body || {};
  const evt = body.evt || body;
  const result = applyEspnEvent(evt);
  return res.json({ ok: true, result });
});

app.options('/provider/:provider/draft-event', (_req, res) => {
  setBridgeCors(res);
  return res.sendStatus(204);
});

app.post('/provider/:provider/draft-event', (req, res) => {
  setBridgeCors(res);

  if (!requireBridgeAuth(req, res)) return;

  const body = req.body || {};
  const evt = body.evt || body;
  const provider = req.params.provider || body.provider || evt.provider || 'unknown';
  const result = applyProviderEvent({ ...evt, provider });

  return res.json({ ok: true, result });
});

app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'draft-dashboard.html'));
});

app.listen(PORT, () => {
  console.log(`Live draft dashboard listening on http://localhost:${PORT}`);
});
