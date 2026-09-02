function firstArray(...values) {
  for (const v of values) {
    if (Array.isArray(v)) return v;
  }
  return [];
}

function firstObject(...values) {
  for (const v of values) {
    if (v && typeof v === 'object' && !Array.isArray(v)) return v;
  }
  return {};
}

export function normalizeLeagueMeta(leagueInfoData, leagueId) {
  const root = firstObject(leagueInfoData);
  const teamInfo = firstObject(root.teamInfo, root.teams, root.teamMap);
  const leagueName = root.leagueName || root.name || null;
  const seasonYear = root.seasonYear || root.year || null;

  const teams = Object.entries(teamInfo).map(([teamId, team]) => ({
    teamId,
    name: team?.name || team?.teamName || `Team ${teamId}`,
    raw: team
  }));

  return {
    provider: 'fantrax',
    type: 'LEAGUE_META',
    leagueId,
    receivedAt: new Date().toISOString(),
    leagueName,
    seasonYear,
    teams,
    raw: root
  };
}

export function extractDraftPicks(data) {
  const root = firstObject(data);
  return firstArray(root.draftPicks, root.picks, root.results);
}

export function extractTeamRosters(data) {
  const root = firstObject(data);
  const teamMap = firstObject(root.teamRosters, root.rosters, root.teams);
  return teamMap;
}

export function normalizeFantraxPick(rawPick, leagueId) {
  const round = Number(rawPick.round ?? rawPick.roundNumber ?? null) || null;
  const overallPick = Number(rawPick.pick ?? rawPick.overallPick ?? null) || null;
  const pickInRound = Number(rawPick.pickInRound ?? rawPick.roundPick ?? null) || null;
  const teamId = String(rawPick.teamId ?? rawPick.franchiseId ?? rawPick.ownerId ?? '').trim() || null;
  const playerId = String(rawPick.playerId ?? rawPick.id ?? '').trim() || null;
  const receivedAt = rawPick.time || rawPick.timestamp || new Date().toISOString();

  return {
    provider: 'fantrax',
    type: 'SELECTED',
    leagueId,
    receivedAt,
    teamId,
    playerId,
    overallPick,
    round,
    pickInRound,
    clockMs: null,
    onTheClockTeamId: null,
    raw: rawPick
  };
}

export function normalizeFantraxRosterPicks(teamRosters, leagueId) {
  const out = [];

  for (const [teamId, roster] of Object.entries(teamRosters || {})) {
    const players = firstArray(
      roster?.players,
      roster?.roster,
      roster?.draftResults,
      roster?.draftedPlayers
    );

    for (const p of players) {
      const draft = firstObject(p?.draft, p?.draftInfo, p?.pickInfo);
      const round = Number(draft.round ?? p.round ?? null) || null;
      const overallPick = Number(draft.pick ?? p.pick ?? p.overallPick ?? null) || null;
      const pickInRound = Number(draft.pickInRound ?? p.pickInRound ?? null) || null;
      const playerId = String(p.playerId ?? p.id ?? '').trim() || null;
      const receivedAt = draft.time || p.time || new Date().toISOString();

      if (!playerId) continue;

      out.push({
        provider: 'fantrax',
        type: 'SELECTED',
        leagueId,
        receivedAt,
        teamId,
        playerId,
        overallPick,
        round,
        pickInRound,
        clockMs: null,
        onTheClockTeamId: null,
        raw: { teamId, player: p }
      });
    }
  }

  return out;
}

export function pickKey(evt) {
  return [
    evt.leagueId || '',
    evt.teamId || '',
    evt.playerId || '',
    evt.overallPick || '',
    evt.round || '',
    evt.pickInRound || ''
  ].join('|');
}
