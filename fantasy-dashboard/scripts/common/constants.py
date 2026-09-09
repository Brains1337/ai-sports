"""Canonical vocabulary for the whole platform.

Every cross-script string literal lives here. This module exists because the
platform's dominant failure mode was silent literal mismatch: ESPN wrote
``platform='espn'`` while the views and the waiver planner read
``'espn-nfl'``, so joins matched nothing and every consumer returned zero
rows without an error. If a value appears in two scripts, it belongs in this
file and nowhere else.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Enum whose members are usable directly as SQL bind parameters."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# ---------------------------------------------------------------------------
# Platform / sport / scoring
# ---------------------------------------------------------------------------


class Platform(StrEnum):
    ESPN_NFL = "espn-nfl"
    ESPN_PICKEM = "espn-pickem"
    YAHOO_NFL = "yahoo-nfl"
    YAHOO_CFB = "yahoo-cfb"
    FANTRAX_NFL = "fantrax-nfl"
    FANTRAX_CFB = "fantrax-cfb"


class Sport(StrEnum):
    NFL = "NFL"
    NCAAF = "NCAAF"


class ScoringType(StrEnum):
    STD = "STD"
    HALF_PPR = "HALF_PPR"
    PPR = "PPR"
    PICKEM = "PICKEM"


#: Reception value per scoring type — the only thing that distinguishes them
#: at the stat level, and the reason storing the wrong one matters.
RECEPTION_POINTS = {
    ScoringType.STD: 0.0,
    ScoringType.HALF_PPR: 0.5,
    ScoringType.PPR: 1.0,
}

#: Platforms that hold rosters. `espn-pickem` deliberately excluded — it has
#: no players, and including it produced a phantom league in every report.
ROSTER_PLATFORMS = frozenset(
    {
        Platform.ESPN_NFL,
        Platform.YAHOO_NFL,
        Platform.YAHOO_CFB,
        Platform.FANTRAX_NFL,
        Platform.FANTRAX_CFB,
    }
)

#: Platforms whose external league/player ids are NOT numeric. Fantrax league
#: ids look like "2hbybmp6msnsbuqa"; player ids are likewise alphanumeric, so
#: they must use *_key text columns rather than the bigint columns.
NON_NUMERIC_ID_PLATFORMS = frozenset({Platform.FANTRAX_CFB, Platform.FANTRAX_NFL})


# ---------------------------------------------------------------------------
# Roster state
# ---------------------------------------------------------------------------


class RosterStatus(StrEnum):
    """Does anyone own this player? Orthogonal to :class:`LineupStatus`."""

    OWNED = "owned"
    WAIVERS = "waivers"
    FREE_AGENT = "free_agent"


class LineupStatus(StrEnum):
    """Where an owned player sits. Previously conflated into RosterStatus,
    which is why Fantrax IR/taxi/minors all collapsed to 'bench' and ESPN
    threw away lineupSlotId entirely."""

    STARTER = "starter"
    BENCH = "bench"
    IR = "ir"
    TAXI = "taxi"


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

#: Canonical position codes. Team defense is **DEF** everywhere.
#:
#: The live database had three different literals for one concept: ESPN
#: stored ``pos='DEF'``, ``league_slots`` used ``'D/ST'``, and FantasyPros was
#: queried for ``'DST'``. Normalize on DEF and translate at the edges.
POS_QB = "QB"
POS_RB = "RB"
POS_WR = "WR"
POS_TE = "TE"
POS_K = "K"
POS_DEF = "DEF"

FANTASY_POSITIONS = (POS_QB, POS_RB, POS_WR, POS_TE, POS_K, POS_DEF)

#: Provider position spellings -> canonical.
POS_ALIASES = {
    "DST": POS_DEF,
    "D/ST": POS_DEF,
    "DEF": POS_DEF,
    "TMD": POS_DEF,
    "TM": POS_DEF,
    "ST": POS_DEF,
    "PK": POS_K,
    "K": POS_K,
    "FB": POS_RB,
    "PLACEKICKER": POS_K,
}

#: Slot names that accept more than one position. Needed by the lineup
#: optimizer — a greedy fill mis-assigns multi-eligible slots.
SLOT_ELIGIBILITY = {
    "QB": (POS_QB,),
    "RB": (POS_RB,),
    "WR": (POS_WR,),
    "TE": (POS_TE,),
    "K": (POS_K,),
    "DEF": (POS_DEF,),
    "D/ST": (POS_DEF,),
    "RB/WR": (POS_RB, POS_WR),
    "WR/TE": (POS_WR, POS_TE),
    "FLEX": (POS_RB, POS_WR, POS_TE),
    "OP": (POS_QB, POS_RB, POS_WR, POS_TE),
    "SUPERFLEX": (POS_QB, POS_RB, POS_WR, POS_TE),
}

#: Slots that never score.
NON_SCORING_SLOTS = frozenset({"BN", "BE", "BENCH", "IR", "IL", "TAXI", "RES"})


def canonical_pos(raw: str | None) -> str | None:
    """Map a provider position string to a canonical code.

    Returns ``None`` for unknown/blank input rather than inventing a value —
    the live data contains ``'UNKNOWN'`` (419 ESPN rows), empty strings, and
    Fantrax artifacts like ``'LOG'`` and ``'Default'``, none of which should
    silently become a real position.
    """
    if not raw:
        return None
    key = raw.strip().upper()
    if not key or key in {"UNKNOWN", "DEFAULT", "NA", "-"}:
        return None
    return POS_ALIASES.get(key, key)


# ---------------------------------------------------------------------------
# Projection / ranking sources
# ---------------------------------------------------------------------------


class SourceName(StrEnum):
    FANTASYPROS_PROJ = "fantasypros_proj"
    FANTASYPROS_ECR = "fantasypros_ecr"
    CFBD_PROJ_YAHOO = "cfbd_cfb_proj_yahoo"
    CFBD_PROJ_FANTRAX = "cfbd_cfb_proj_fantrax"
    FANTASYPROS = "fantasypros"
    CFBD = "cfbd"


#: Which projection source feeds which platform. Mirrors the CASE expression
#: inside the `league_rosters` / `waiver_wire` views — keep the two in sync,
#: or better, have migration 018 generate the view from this mapping.
PLATFORM_PROJECTION_SOURCE = {
    Platform.ESPN_NFL: SourceName.FANTASYPROS_PROJ,
    Platform.YAHOO_NFL: SourceName.FANTASYPROS_PROJ,
    Platform.FANTRAX_NFL: SourceName.FANTASYPROS_PROJ,
    Platform.YAHOO_CFB: SourceName.CFBD_PROJ_YAHOO,
    Platform.FANTRAX_CFB: SourceName.CFBD_PROJ_FANTRAX,
}


# ---------------------------------------------------------------------------
# Weeks
# ---------------------------------------------------------------------------

#: ``week = 0`` means season-long. This matches FantasyPros' own sentinel, so
#: the value passes through their API unchanged.
SEASON_LONG_WEEK = 0


# ---------------------------------------------------------------------------
# Matching thresholds
# ---------------------------------------------------------------------------

#: Below this confidence a match is refused and the row is queued for review.
#:
#: The old matcher initialised ``best_score = -1``, so *any* candidate scored
#: higher and won — it could never return "no match". That is how 94
#: FantasyPros players bound onto 47 unrelated database rows.
MATCH_CONFIDENCE_FLOOR = 0.70

#: Confidence assigned per match tier.
MATCH_CONFIDENCE = {
    "exact_name_team_pos": 1.00,
    "exact_name_pos": 0.90,
    "key_name_pos": 0.85,
    "exact_name": 0.75,
    "fuzzy_pos": 0.72,
}
