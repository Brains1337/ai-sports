const FANTRAX_BASE = 'https://www.fantrax.com/fxea/general';

function buildHeaders(cookie) {
  const headers = {
    'accept': 'application/json, text/plain, */*',
    'user-agent': 'Mozilla/5.0'
  };
  if (cookie) headers.cookie = cookie;
  return headers;
}

async function requestJson(url, { cookie, timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, {
      method: 'GET',
      headers: buildHeaders(cookie),
      signal: controller.signal
    });

    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { rawText: text };
    }

    if (!res.ok) {
      const err = new Error(`Fantrax request failed ${res.status} for ${url}`);
      err.status = res.status;
      err.body = data;
      throw err;
    }

    return data;
  } finally {
    clearTimeout(timer);
  }
}

export function getLeagueInfo({ leagueId, cookie }) {
  const url = `${FANTRAX_BASE}/getLeagueInfo?leagueId=${encodeURIComponent(leagueId)}`;
  return requestJson(url, { cookie });
}

export function getDraftPicks({ leagueId, cookie }) {
  const url = `${FANTRAX_BASE}/getDraftPicks?leagueId=${encodeURIComponent(leagueId)}`;
  return requestJson(url, { cookie });
}

export function getTeamRosters({ leagueId, cookie, period = 6 }) {
  const url = `${FANTRAX_BASE}/getTeamRosters?leagueId=${encodeURIComponent(leagueId)}&period=${encodeURIComponent(period)}`;
  return requestJson(url, { cookie });
}

export async function getLeagueSnapshot({ leagueId, cookie }) {
  const [leagueInfo, draftPicks, teamRosters] = await Promise.allSettled([
    getLeagueInfo({ leagueId, cookie }),
    getDraftPicks({ leagueId, cookie }),
    getTeamRosters({ leagueId, cookie, period: 6 })
  ]);

  return {
    leagueId,
    leagueInfo,
    draftPicks,
    teamRosters,
    fetchedAt: new Date().toISOString()
  };
}
