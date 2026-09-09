"""Tests for common.matching — the module that replaced a matcher which
could not fail.

The regression these guard against, in production terms: 94 FantasyPros
player keys bound onto 47 arbitrary `players.id` rows, with zero overlap
between those 47 and the 415 actually-rostered players.

Run:  pytest tests/ -v
The SQL-parity test is skipped unless DATABASE_URL is set.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common.constants import canonical_pos  # noqa: E402
from common.matching import (  # noqa: E402
    SQL_FOLD_FROM,
    SQL_FOLD_TO,
    MatchStats,
    _token_set_ratio,
    name_key,
    normalize_name,
)

# Names that specifically defeated the old matcher: every one contains
# punctuation, so `normalize_name(x) == lower(player_name)` was always false
# and the code fell through to a first-name LIKE.
PUNCTUATED = [
    ("A.J. Brown", "aj brown", "aj brown"),
    ("D'Andre Swift", "dandre swift", "dandre swift"),
    ("Amon-Ra St. Brown", "amonra st brown", "amonra st brown"),
    ("Ja'Marr Chase", "jamarr chase", "jamarr chase"),
    ("Michael Penix Jr.", "michael penix jr", "michael penix"),
    ("Kenneth Walker III", "kenneth walker iii", "kenneth walker"),
    ("Marvin Harrison Jr", "marvin harrison jr", "marvin harrison"),
    ("Odell Beckham Jr.", "odell beckham jr", "odell beckham"),
]


@pytest.mark.parametrize("raw,expect_norm,expect_key", PUNCTUATED)
def test_normalize_and_key(raw, expect_norm, expect_key):
    assert normalize_name(raw) == expect_norm
    assert name_key(raw) == expect_key


def test_normalize_empty_and_none():
    assert normalize_name(None) == ""
    assert normalize_name("") == ""
    assert name_key(None) == ""


def test_accent_folding():
    # Without translate()/NFKD these would lose the accented char entirely
    # and never match the DB's generated column.
    assert normalize_name("José Peña") == "jose pena"
    assert normalize_name("Bøkeberg") == "bokeberg"  # slashed-o: NFKD alone fails
    assert normalize_name("Šimon Žák") == "simon zak"


def test_fold_table_lengths_match():
    """translate() requires equal-length from/to, or Postgres errors."""
    assert len(SQL_FOLD_FROM) == len(SQL_FOLD_TO)


def test_whitespace_collapsed():
    assert normalize_name("  Jimmy   Garoppolo  ") == "jimmy garoppolo"


def test_suffix_only_stripped_at_end():
    # "Vic Beasley" must not lose "v"; only a trailing standalone suffix goes.
    assert name_key("Javon Kinlaw") == "javon kinlaw"
    assert name_key("Robert Griffin III") == "robert griffin"


# ---------------------------------------------------------------------------
# Fuzzy scoring
# ---------------------------------------------------------------------------


def test_token_set_order_insensitive():
    assert _token_set_ratio(normalize_name("Brown, A.J."),
                            normalize_name("A.J. Brown")) == 1.0


def test_token_set_rejects_shared_first_name():
    """The core old bug: 'aj brown' vs 'aj green' must score far below the
    0.92 fuzzy threshold, so it is refused rather than matched."""
    assert _token_set_ratio("aj brown", "aj green") < 0.5


def test_token_set_rejects_common_surname():
    assert _token_set_ratio("mike williams", "mike evans") < 0.5
    assert _token_set_ratio("josh allen", "josh jacobs") < 0.5


def test_token_set_empty():
    assert _token_set_ratio("", "anything") == 0.0


# ---------------------------------------------------------------------------
# Position canonicalization — three literals existed for team defense
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DST", "DEF"), ("D/ST", "DEF"), ("DEF", "DEF"),
        ("TmD", "DEF"), ("ST", "DEF"),
        ("PK", "K"), ("k", "K"),
        ("QB", "QB"), ("  rb  ", "RB"),
        # Live-data junk that must NOT become a real position.
        ("UNKNOWN", None), ("Default", None), ("", None), (None, None),
        ("-", None), ("NA", None),
    ],
)
def test_canonical_pos(raw, expected):
    assert canonical_pos(raw) == expected


def test_canonical_pos_passes_through_unknown_codes():
    # Fantrax junk like 'LOG'/'OL' stays distinct rather than being coerced.
    assert canonical_pos("OL") == "OL"
    assert canonical_pos("LOG") == "LOG"


# ---------------------------------------------------------------------------
# MatchStats bookkeeping
# ---------------------------------------------------------------------------


def test_match_stats_records_refusals():
    st = MatchStats()
    st.record(None, "Nobody Real")
    st.record(None, "Also Nobody")
    assert st.refused == 2
    assert st.matched == 0
    assert "Nobody Real" in st.as_meta()["unmatched_sample"]


def test_match_stats_caps_unmatched_sample():
    st = MatchStats()
    for i in range(200):
        st.record(None, f"player {i}")
    assert st.refused == 200
    assert len(st.unmatched_names) <= 50
    assert len(st.as_meta()["unmatched_sample"]) <= 25


# ---------------------------------------------------------------------------
# SQL parity — the important one. Runs the real generated-column expression.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DATABASE_URL")
def test_normalize_matches_sql():
    """Python normalize_name/name_key must equal the SQL generated columns.

    If these drift, exact-tier lookups miss the index and silently degrade to
    the fuzzy tier — quietly reintroducing low-quality matches.
    """
    from sqlalchemy import text

    from common.db import begin

    sql = text(
        """
        select
          btrim(regexp_replace(
              lower(regexp_replace(
                  translate(:n, :ffrom, :fto), '[^A-Za-z0-9 ]', '', 'g')),
              '\\s+', ' ', 'g')) as name_norm,
          btrim(regexp_replace(
              btrim(regexp_replace(
                  lower(regexp_replace(
                      translate(:n, :ffrom, :fto), '[^A-Za-z0-9 ]', '', 'g')),
                  '\\s+', ' ', 'g')),
              '\\s+(jr|sr|ii|iii|iv|v)$', '', 'g')) as name_key
        """
    )

    samples = [raw for raw, _, _ in PUNCTUATED] + [
        "José Peña", "Bøkeberg", "Šimon Žák", "  Jimmy   Garoppolo  ",
        "Robert Griffin III", "Ka'imi Fairbairn", "T.J. Hockenson",
    ]

    with begin() as conn:
        for raw in samples:
            row = conn.execute(
                sql, {"n": raw, "ffrom": SQL_FOLD_FROM, "fto": SQL_FOLD_TO}
            ).mappings().one()
            assert row["name_norm"] == normalize_name(raw), f"name_norm drift on {raw!r}"
            assert row["name_key"] == name_key(raw), f"name_key drift on {raw!r}"


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DATABASE_URL")
def test_generated_columns_agree_with_python_on_real_rows():
    """Sample real players and confirm the stored columns match Python."""
    from sqlalchemy import text

    from common.db import begin

    with begin() as conn:
        rows = conn.execute(
            text(
                """
                select player_name, name_norm, name_key from players
                 where player_name ~ '[^A-Za-z0-9 ]'
                 order by random() limit 300
                """
            )
        ).mappings().all()

    assert rows, "no punctuated player names found — has 013 been applied?"
    for r in rows:
        assert r["name_norm"] == normalize_name(r["player_name"])
        assert r["name_key"] == name_key(r["player_name"])
