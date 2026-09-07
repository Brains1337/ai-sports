#!/usr/bin/env python3
"""
cfb_scoring.py — league-specific NCAAF fantasy scoring formulas.

Two leagues, two very different rule sets:

Yahoo EDIT League (platform='yahoo-cfb'):
  - 0.5 PPR
  - Pass: 25 yd/pt, 4 pt TD, -1 INT
  - Rush: 10 yd/pt, 6 pt TD
  - Rec:  10 yd/pt, 6 pt TD, 0.5/reception
  - Fumbles lost: -2, 2-pt conversions: 2
  (Kicker/DST scoring intentionally omitted here — those are scored
   from box score data separately if/when we add K/DST projections.)

Fantrax New Freshman (platform='fantrax-cfb'):
  - Standard, NO PPR (no points for receptions at all)
  - Pass: 0.04 pt/yd (=25 yd/pt), 4 pt TD
  - Rush: 0.1 pt/yd (=10 yd/pt), 6 pt TD
  - Rec:  0.1 pt/yd (=10 yd/pt), 6 pt TD (no reception bonus)
  - 2-pt conversions: 2, Fumble recovery TD (offense): 6
  (No INT/fumble-lost penalty specified in this league's rules.)
"""

from typing import Any, Dict

YAHOO_CFB = "yahoo-cfb"
FANTRAX_CFB = "fantrax-cfb"


def score_yahoo_cfb(stats: Dict[str, Any]) -> float:
    pass_yd = float(stats.get("pass_yd") or 0)
    pass_td = float(stats.get("pass_td") or 0)
    interceptions = float(stats.get("interceptions") or 0)

    rush_yd = float(stats.get("rush_yd") or 0)
    rush_td = float(stats.get("rush_td") or 0)

    rec = float(stats.get("receptions") or 0)
    rec_yd = float(stats.get("rec_yd") or 0)
    rec_td = float(stats.get("rec_td") or 0)

    fumbles_lost = float(stats.get("fumbles_lost") or 0)
    two_pt = float(stats.get("two_pt_conversions") or 0)

    points = 0.0
    points += pass_yd / 25.0
    points += pass_td * 4.0
    points += interceptions * -1.0

    points += rush_yd / 10.0
    points += rush_td * 6.0

    points += rec * 0.5
    points += rec_yd / 10.0
    points += rec_td * 6.0

    points += fumbles_lost * -2.0
    points += two_pt * 2.0

    return round(points, 2)


def score_fantrax_cfb(stats: Dict[str, Any]) -> float:
    pass_yd = float(stats.get("pass_yd") or 0)
    pass_td = float(stats.get("pass_td") or 0)

    rush_yd = float(stats.get("rush_yd") or 0)
    rush_td = float(stats.get("rush_td") or 0)

    rec_yd = float(stats.get("rec_yd") or 0)
    rec_td = float(stats.get("rec_td") or 0)

    two_pt = float(stats.get("two_pt_conversions") or 0)
    fumble_ret_td = float(stats.get("fumble_return_td") or 0)

    points = 0.0
    points += pass_yd * 0.04
    points += pass_td * 4.0

    points += rush_yd * 0.1
    points += rush_td * 6.0

    # No reception bonus in this league — receptions intentionally excluded.
    points += rec_yd * 0.1
    points += rec_td * 6.0

    points += two_pt * 2.0
    points += fumble_ret_td * 6.0

    return round(points, 2)


SCORERS = {
    YAHOO_CFB: score_yahoo_cfb,
    FANTRAX_CFB: score_fantrax_cfb,
}


def score_stats(platform: str, stats: Dict[str, Any]) -> float:
    scorer = SCORERS.get(platform)
    if not scorer:
        raise ValueError(f"No scorer configured for platform={platform}")
    return scorer(stats)
