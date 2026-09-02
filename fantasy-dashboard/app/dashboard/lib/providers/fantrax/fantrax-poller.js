import { getLeagueSnapshot } from './fantrax-client.js';
import {
  normalizeLeagueMeta,
  extractDraftPicks,
  extractTeamRosters,
  normalizeFantraxPick,
  normalizeFantraxRosterPicks,
  pickKey
} from './fantrax-normalize.js';
import { ensureLeagueState } from './fantrax-state.js';

function settledValue(result) {
  return result?.status === 'fulfilled' ? result.value : null;
}

function hashJson(value) {
  return JSON.stringify(value || null);
}

export async function pollFantraxLeague({ leagueId, cookie, state, onEvent, logger = console }) {
  const leagueState = ensureLeagueState(state, leagueId);
  leagueState.lastPollAt = new Date().toISOString();

  try {
    const snapshot = await getLeagueSnapshot({ leagueId, cookie });

    const leagueInfoData = settledValue(snapshot.leagueInfo);
    const draftPicksData = settledValue(snapshot.draftPicks);
    const teamRostersData = settledValue(snapshot.teamRosters);

    if (leagueInfoData) {
      const meta = normalizeLeagueMeta(leagueInfoData, leagueId);
      const metaHash = hashJson({
        leagueName: meta.leagueName,
        seasonYear: meta.seasonYear,
        teams: meta.teams.map(t => ({ teamId: t.teamId, name: t.name }))
      });

      if (metaHash !== leagueState.lastLeagueMetaHash) {
        leagueState.lastLeagueMetaHash = metaHash;
        await onEvent(meta);
      }
    }

    const draftPickEvents = extractDraftPicks(draftPicksData).map(p => normalizeFantraxPick(p, leagueId));
    const rosterPickEvents = normalizeFantraxRosterPicks(extractTeamRosters(teamRostersData), leagueId);

    const merged = [...draftPickEvents, ...rosterPickEvents]
      .filter(evt => evt.playerId && evt.teamId)
      .sort((a, b) => {
        const ao = a.overallPick ?? Number.MAX_SAFE_INTEGER;
        const bo = b.overallPick ?? Number.MAX_SAFE_INTEGER;
        if (ao !== bo) return ao - bo;
        return String(a.receivedAt).localeCompare(String(b.receivedAt));
      });

    for (const evt of merged) {
      const key = pickKey(evt);
      if (leagueState.seenPickKeys.has(key)) continue;
      leagueState.seenPickKeys.add(key);
      await onEvent(evt);
    }

    leagueState.lastSuccessAt = new Date().toISOString();
    leagueState.lastError = null;

    logger.info?.(`[fantrax] polled ${leagueId} ok; emitted=${merged.length}`);
    return { ok: true, leagueId, emitted: merged.length };
  } catch (error) {
    leagueState.lastError = {
      at: new Date().toISOString(),
      message: error.message
    };
    logger.error?.(`[fantrax] poll failed ${leagueId}: ${error.message}`);
    return { ok: false, leagueId, error: error.message };
  }
}

export async function pollFantraxLeagues({ leagueIds, cookie, state, onEvent, logger = console }) {
  const results = [];
  for (const leagueId of leagueIds) {
    results.push(await pollFantraxLeague({ leagueId, cookie, state, onEvent, logger }));
  }
  return results;
}
