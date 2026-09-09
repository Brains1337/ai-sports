"""Player name matching — one implementation, used by every sync script.

Replaces three divergent matchers, the worst of which could not fail. The
old ``find_best_player_match`` in ``sync_fantasypros.py``:

* compared a punctuation-stripped needle (``'aj brown'``) against an
  unstripped haystack (``lower('A.J. Brown')``), so the exact branch never
  fired for any name containing ``.``, ``'`` or ``-``;
* fell through to ``LIKE '%aj%'``, matching any name containing that
  substring, ordered by ``percent_owned`` — returning the most *popular*
  unrelated player;
* initialised ``best_score = -1``, so any candidate scored higher and won.
  It had no "no match" outcome at all.

The observable result in production: 94 FantasyPros source keys bound onto
47 arbitrary ``players.id`` rows, and zero overlap between those 47 and the
415 actually-rostered players.

The rule here is the inverse: **refuse rather than guess.** A row below
:data:`MATCH_CONFIDENCE_FLOOR` returns ``None`` and is queued for review.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import text

from .constants import (
    MATCH_CONFIDENCE,
    MATCH_CONFIDENCE_FLOOR,
    canonical_pos,
)

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")

# Accent fold table. MUST stay identical to the translate() call in the
# players.name_norm / name_key generated columns (migration 013), or indexed
# lookups silently miss and every match degrades to the fuzzy tier.
#
# PostgreSQL cannot use unaccent() in a generated column (it is only STABLE;
# generated expressions require IMMUTABLE), so the DB side uses translate(),
# which is IMMUTABLE. NFKD alone is NOT equivalent to that table -- it leaves
# 'ø' and 'Ø' undecomposed, where translate() folds them to o/O. Applying the
# same table here after NFKD makes the two sides agree exactly.
_FOLD_GROUPS = {
    "a": "áàâäãåā",
    "e": "éèêëē",
    "i": "íìîïī",
    "o": "óòôöõøō",
    "u": "úùûüū",
    "n": "ñ",
    "c": "ç",
    "y": "ýÿ",
    "s": "š",
    "z": "ž",
}
_FOLD_SRC = "".join(ch for chars in _FOLD_GROUPS.values() for ch in chars)
_FOLD_DST = "".join(tgt for tgt, chars in _FOLD_GROUPS.items() for _ in chars)
SQL_FOLD_FROM = _FOLD_SRC + _FOLD_SRC.upper()
SQL_FOLD_TO = _FOLD_DST + _FOLD_DST.upper()
_FOLD_TABLE = str.maketrans(SQL_FOLD_FROM, SQL_FOLD_TO)


def normalize_name(name: str | None) -> str:
    """Fold to ASCII, strip punctuation, collapse whitespace, lowercase.

    Byte-identical to the ``players.name_norm`` generated column. Verified by
    ``tests/test_matching.py::test_normalize_matches_sql``, which runs the
    real SQL expression against the same inputs.
    """
    if not name:
        return ""
    folded = name.translate(_FOLD_TABLE)
    decomposed = unicodedata.normalize("NFKD", folded)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower().strip()
    return _WS_RE.sub(" ", _PUNCT_RE.sub("", lowered)).strip()


def name_key(name: str | None) -> str:
    """:func:`normalize_name` with a trailing generational suffix removed.

    Mirrors ``players.name_key``. Handles the common real-world mismatch
    where one provider writes "Michael Penix Jr" and another "Michael Penix".
    """
    norm = normalize_name(name)
    if not norm:
        return ""
    parts = norm.split(" ")
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(parts)


def _token_set_ratio(a: str, b: str) -> float:
    """Jaccard similarity over word tokens, in 0..1.

    Deliberately dependency-free — ``rapidfuzz`` would be better but the sync
    containers install their requirements at boot and this avoids adding a
    wheel to every one of them. Order-insensitive, so "Brown, A.J." and
    "A.J. Brown" score identically.
    """
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class MatchResult:
    player_id: int
    player_name: str
    pos: str | None
    confidence: float
    tier: str
    ambiguous_with: int = 0


@dataclass
class MatchStats:
    """Aggregate outcome of a matching pass, for ``sync_runs.meta``."""

    matched: int = 0
    refused: int = 0
    ambiguous: int = 0
    by_tier: dict[str, int] = field(default_factory=dict)
    unmatched_names: list[str] = field(default_factory=list)

    def record(self, result: MatchResult | None, source_name: str) -> None:
        if result is None:
            self.refused += 1
            if len(self.unmatched_names) < 50:
                self.unmatched_names.append(source_name)
            return
        self.matched += 1
        self.by_tier[result.tier] = self.by_tier.get(result.tier, 0) + 1
        if result.ambiguous_with:
            self.ambiguous += 1

    def as_meta(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "refused": self.refused,
            "ambiguous": self.ambiguous,
            "by_tier": self.by_tier,
            "unmatched_sample": self.unmatched_names[:25],
        }


class PlayerMatcher:
    """Matches provider player rows to ``players.id`` for one platform.

    Loads the platform's players once into memory and indexes them, rather
    than issuing a query per player. The old matcher ran one ``LIKE`` scan
    per player per position, with ``limit 20`` — which could truncate the
    correct answer out of the candidate set entirely.
    """

    def __init__(self, conn: Any, platform: str, sport: str | None = None) -> None:
        self.platform = str(platform)
        self._by_norm_pos: dict[tuple[str, str], list[dict]] = {}
        self._by_key_pos: dict[tuple[str, str], list[dict]] = {}
        self._by_norm: dict[str, list[dict]] = {}
        self._all: list[dict] = []

        sql = """
            select id, player_name, name_norm, name_key, pos, pro_team_id,
                   percent_owned
              from players
             where platform = :platform
        """
        params: dict[str, Any] = {"platform": self.platform}
        if sport:
            sql += " and sport = :sport"
            params["sport"] = str(sport)

        for row in conn.execute(text(sql), params).mappings():
            rec = dict(row)
            rec["_pos"] = canonical_pos(rec.get("pos"))
            self._all.append(rec)

            norm = rec.get("name_norm") or normalize_name(rec["player_name"])
            key = rec.get("name_key") or name_key(rec["player_name"])
            pos = rec["_pos"] or ""

            self._by_norm_pos.setdefault((norm, pos), []).append(rec)
            self._by_key_pos.setdefault((key, pos), []).append(rec)
            self._by_norm.setdefault(norm, []).append(rec)

    def __len__(self) -> int:
        return len(self._all)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _pick(
        candidates: Sequence[dict],
        team_id: int | None,
    ) -> tuple[dict | None, int]:
        """Choose among equally-named candidates.

        Returns ``(chosen, n_ambiguous)``. A pro-team match breaks the tie;
        otherwise, if several remain, we still return one but report the
        ambiguity so it lands in ``sync_runs.meta`` and can be reviewed. We
        do *not* fall back to "highest percent_owned" — that heuristic is
        precisely what made the old matcher pick famous strangers.
        """
        if not candidates:
            return None, 0
        if len(candidates) == 1:
            return candidates[0], 0
        if team_id is not None:
            exact = [c for c in candidates if c.get("pro_team_id") == team_id]
            if len(exact) == 1:
                return exact[0], 0
            if exact:
                return exact[0], len(exact) - 1
        return candidates[0], len(candidates) - 1

    # -- public ------------------------------------------------------------

    def match(
        self,
        source_name: str,
        *,
        pos: str | None = None,
        team_id: int | None = None,
        fuzzy_threshold: float = 0.92,
    ) -> MatchResult | None:
        """Resolve one provider name, or return ``None``.

        Tiers, highest confidence first:

        1. exact ``name_norm`` + position (+ team tiebreak)  -> 0.90-1.00
        2. suffix-insensitive ``name_key`` + position        -> 0.85
        3. exact ``name_norm``, no position given            -> 0.75
        4. token-set fuzzy >= ``fuzzy_threshold``, same pos   -> 0.72

        Anything weaker is refused.
        """
        norm = normalize_name(source_name)
        if not norm:
            return None

        cpos = canonical_pos(pos) or ""

        def build(rec: dict, tier: str, ambiguous: int) -> MatchResult:
            conf = MATCH_CONFIDENCE[tier]
            if ambiguous:
                # Ambiguity is a real reduction in certainty, not cosmetic.
                conf = round(conf - 0.05 * min(ambiguous, 3), 3)
            return MatchResult(
                player_id=rec["id"],
                player_name=rec["player_name"],
                pos=rec.get("_pos"),
                confidence=conf,
                tier=tier,
                ambiguous_with=ambiguous,
            )

        # Tier 1 -----------------------------------------------------------
        if cpos:
            rec, amb = self._pick(self._by_norm_pos.get((norm, cpos), []), team_id)
            if rec:
                tier = "exact_name_team_pos" if (
                    team_id is not None and rec.get("pro_team_id") == team_id
                ) else "exact_name_pos"
                return build(rec, tier, amb)

        # Tier 2 -----------------------------------------------------------
        if cpos:
            nkey = name_key(source_name)
            rec, amb = self._pick(self._by_key_pos.get((nkey, cpos), []), team_id)
            if rec:
                return build(rec, "key_name_pos", amb)

        # Tier 3 -----------------------------------------------------------
        rec, amb = self._pick(self._by_norm.get(norm, []), team_id)
        if rec:
            # Refuse a name-only hit that contradicts a stated position.
            if cpos and rec.get("_pos") and rec["_pos"] != cpos:
                return None
            return build(rec, "exact_name", amb)

        # Tier 4 -----------------------------------------------------------
        if cpos:
            best: dict | None = None
            best_ratio = 0.0
            for cand in self._all:
                if cand.get("_pos") != cpos:
                    continue
                ratio = _token_set_ratio(norm, cand.get("name_norm") or "")
                if ratio > best_ratio:
                    best, best_ratio = cand, ratio
            if best is not None and best_ratio >= fuzzy_threshold:
                return build(best, "fuzzy_pos", 0)

        return None

    def match_or_refuse(
        self,
        source_name: str,
        stats: MatchStats,
        **kwargs: Any,
    ) -> MatchResult | None:
        """:meth:`match`, recording the outcome and enforcing the floor."""
        result = self.match(source_name, **kwargs)
        if result is not None and result.confidence < MATCH_CONFIDENCE_FLOOR:
            result = None
        stats.record(result, source_name)
        return result


def upsert_xref(
    conn: Any,
    *,
    player_id: int,
    source_name: str,
    source_player_key: str,
    source_player_name: str | None,
    source_team: str | None,
    source_pos: str | None,
    confidence: float,
    payload: str,
    match_tier: str | None = None,
) -> None:
    """Record the mapping and how confident we were, so it stays auditable.

    Confidence is on a 0..1 scale (migration 013 rescaled the legacy 0..100
    rows). Storing the tier means a later pass can re-examine weak matches
    instead of trusting them forever — the mistake in
    ``sync_cfbd_player_xref.py``, which filters on ``cfbd_athlete_id IS NULL``
    and therefore never revisits a wrong mapping.
    """
    conn.execute(
        text(
            """
            insert into player_xref (
                player_id, source_name, source_player_key, source_player_name,
                source_team, source_pos, confidence, payload
            ) values (
                :player_id, :source_name, :source_player_key, :source_player_name,
                :source_team, :source_pos, :confidence,
                cast(:payload as jsonb) || jsonb_build_object('match_tier', :match_tier)
            )
            on conflict (source_name, source_player_key) do update set
                player_id          = excluded.player_id,
                source_player_name = excluded.source_player_name,
                source_team        = excluded.source_team,
                source_pos         = excluded.source_pos,
                confidence         = excluded.confidence,
                payload            = excluded.payload,
                updated_at         = now()
            """
        ),
        {
            "player_id": player_id,
            "source_name": str(source_name),
            "source_player_key": str(source_player_key),
            "source_player_name": source_player_name,
            "source_team": source_team,
            "source_pos": source_pos,
            "confidence": float(confidence),
            "payload": payload,
            "match_tier": match_tier,
        },
    )


def find_collapses(conn: Any, source_name: str) -> list[tuple[int, int]]:
    """Source keys that collapsed onto a single ``player_id``.

    A non-empty result means several distinct provider players were mapped to
    one database row — the bug this module exists to prevent. Worth asserting
    on after every matching pass.
    """
    rows = conn.execute(
        text(
            """
            select player_id, count(*) AS n
              from player_xref
             where source_name = :source_name
             group by 1
            having count(*) > 1
             order by 2 desc
            """
        ),
        {"source_name": str(source_name)},
    ).all()
    return [(int(r[0]), int(r[1])) for r in rows]
