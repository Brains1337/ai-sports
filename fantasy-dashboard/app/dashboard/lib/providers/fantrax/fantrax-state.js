export function makeFantraxLeagueState(leagueId) {
  return {
    leagueId,
    seenPickKeys: new Set(),
    lastLeagueMetaHash: null,
    lastPollAt: null,
    lastSuccessAt: null,
    lastError: null
  };
}

export function makeFantraxState(leagueIds = []) {
  return {
    leagues: Object.fromEntries(leagueIds.map(id => [id, makeFantraxLeagueState(id)]))
  };
}

export function ensureLeagueState(state, leagueId) {
  if (!state.leagues[leagueId]) {
    state.leagues[leagueId] = makeFantraxLeagueState(leagueId);
  }
  return state.leagues[leagueId];
}
