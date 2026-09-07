#!/usr/bin/env python3
"""
teams_normalizer.py — shared helpers for normalizing college team / DST labels.

Used by:
  - sync_yahoo.py       → normalize DEF team names (Yahoo DST)
  - sync_fantrax.py     → normalize DEF team names (Fantrax DST)
  - sync_cfbd_player_xref.py → normalize team labels for CFBD joins (optional)
"""

from typing import Optional

# Yahoo / CFBD abbreviations → CFBD roster team names
TEAM_CODE_TO_NAME = {
    "ARIZ": "Arizona",
    "ASU": "Arizona State",
    "AUB": "Auburn",
    "BAY": "Baylor",
    "BC": "Boston College",
    "BYU": "BYU",
    "CAL": "California",
    "CIN": "Cincinnati",
    "COLO": "Colorado",
    "CONC": "Concordia",  # verify - unusual for FBS, may be niche/UDFA feed
    "DUKE": "Duke",
    "FLA": "Florida",
    "FSU": "Florida State",
    "GT": "Georgia Tech",
    "HOU": "Houston",
    "ILL": "Illinois",
    "IND": "Indiana",
    "IOWA": "Iowa",
    "ISU": "Iowa State",
    "KSU": "Kansas State",
    "KU": "Kansas",
    "LOU": "Louisville",
    "LSU": "LSU",
    "MEM": "Memphis",
    "MIA": "Miami",
    "MICH": "Michigan",
    "MINN": "Minnesota",
    "MISS": "Ole Miss",
    "MIZZ": "Missouri",
    "MSST": "Mississippi State",
    "MSU": "Michigan State",
    "NCST": "NC State",
    "ND": "Notre Dame",
    "NEB": "Nebraska",
    "NW": "Northwestern",
    "OKST": "Oklahoma State",
    "ORE": "Oregon",
    "OSU": "Ohio State",
    "OU": "Oklahoma",
    "PITT": "Pittsburgh",
    "PSU": "Penn State",
    "PUR": "Purdue",
    "RUTG": "Rutgers",
    "SC": "South Carolina",
    "SMU": "SMU",
    "STAN": "Stanford",
    "SYR": "Syracuse",
    "TAMU": "Texas A&M",
    "TCU": "TCU",
    "TENN": "Tennessee",
    "TEX": "Texas",
    "TTU": "Texas Tech",
    "UCF": "UCF",
    "UCLA": "UCLA",
    "UGA": "Georgia",
    "UK": "Kentucky",
    "UMD": "Maryland",
    "UNC": "North Carolina",
    "USC": "USC",
    "USD": "San Diego",  # verify - FCS, may be data noise
    "UTAH": "Utah",
    "UVA": "Virginia",
    "UW": "Washington",
    "VAN": "Vanderbilt",
    "VT": "Virginia Tech",
    "WAKE": "Wake Forest",
    "WIS": "Wisconsin",
    "WVU": "West Virginia",
    "ALA": "Alabama",
    "AF": "Air Force",
    "ARK": "Arkansas",
    "ARKST": "Arkansas State",
}

# Fantrax DST labels / shorthand player_name strings → CFBD roster team names
DST_LABEL_TO_NAME = {
    # ── confirmed from live Fantrax data (Sep 2026) ──────────────────────────
    "Ind": "Indiana",  # ← was missing; Fantrax uses "Ind" not "IND"
    "Iowa": "Iowa",  # ← was missing; exact-case label from Fantrax
    "MiaFL": "Miami",
    "ND": "Notre Dame",
    "Okla": "Oklahoma",
    "Oreg": "Oregon",
    "OSU": "Ohio State",
    "Tex": "Texas",
    "TxTch": "Texas Tech",
    "UGA": "Georgia",
    # ── full FBS map ─────────────────────────────────────────────────────────
    "AF": "Air Force",
    "Ala": "Alabama",
    "Bama": "Alabama",
    "App": "Appalachian State",
    "Ariz": "Arizona",
    "ArizSt": "Arizona State",
    "Ark": "Arkansas",
    "ArkSt": "Arkansas State",
    "Aub": "Auburn",
    "Ball": "Ball State",
    "Bay": "Baylor",
    "BC": "Boston College",
    "Boise": "Boise State",
    "Buff": "Buffalo",
    "BYU": "BYU",
    "Cal": "California",
    "C Mi": "Central Michigan",
    "Cin": "Cincinnati",
    "Clem": "Clemson",
    "CoCar": "Coastal Carolina",
    "Colo": "Colorado",
    "CSU": "Colorado State",
    "Duke": "Duke",
    "EMU": "Eastern Michigan",
    "Fla": "Florida",
    "FloSt": "Florida State",
    "FreSt": "Fresno State",
    "GT": "Georgia Tech",
    "Haw": "Hawaii",
    "Hou": "Houston",
    "Ill": "Illinois",
    "ISU": "Iowa State",
    "JacSt": "Jacksonville State",
    "JMU": "James Madison",
    "Kan": "Kansas",
    "K St": "Kansas State",
    "KSU": "Kansas State",
    "KY": "Kentucky",
    "LaTch": "Louisiana Tech",
    "Lou": "Louisville",
    "LSU": "LSU",
    "Marsh": "Marshall",
    "MD": "Maryland",
    "Mem": "Memphis",
    "MiaOH": "Miami (OH)",
    "Mich": "Michigan",
    "MichSt": "Michigan State",
    "MIN": "Minnesota",
    "Miss": "Ole Miss",
    "MissSt": "Mississippi State",
    "Mizzou": "Missouri",
    "Navy": "Navy",
    "NCSt": "NC State",
    "NDSU": "North Dakota State",
    "Neb": "Nebraska",
    "NIU": "Northern Illinois",
    "NM": "New Mexico",
    "NMSt": "New Mexico State",
    "NW": "Northwestern",
    "OhioU": "Ohio",
    "OkSt": "Oklahoma State",
    "OldDom": "Old Dominion",
    "Pitt": "Pittsburgh",
    "PSU": "Penn State",
    "Pur": "Purdue",
    "Rice": "Rice",
    "Rut": "Rutgers",
    "SacSt": "Sacramento State",
    "SCar": "South Carolina",
    "SDSU": "San Diego State",
    "SMU": "SMU",
    "SoFL": "South Florida",
    "SoMiss": "Southern Miss",
    "Stan": "Stanford",
    "Syr": "Syracuse",
    "TCU": "TCU",
    "Tenn": "Tennessee",
    "TxAM": "Texas A&M",
    "Toled": "Toledo",
    "Troy": "Troy",
    "Tul": "Tulane",
    "Tulsa": "Tulsa",
    "UCF": "UCF",
    "UCLA": "UCLA",
    "UConn": "UConn",
    "UIUC": "Illinois",
    "UMass": "Massachusetts",
    "UNC": "North Carolina",
    "UNLV": "UNLV",
    "USC": "USC",
    "Utah": "Utah",
    "UtSt": "Utah State",
    "UTEP": "UTEP",
    "UTSA": "UTSA",
    "UVA": "Virginia",
    "Van": "Vanderbilt",
    "VaTec": "Virginia Tech",
    "Wake": "Wake Forest",
    "Wash": "Washington",
    "WestMI": "Western Michigan",
    "Wisc": "Wisconsin",
    "WKU": "Western Kentucky",
    "WVU": "West Virginia",
}


def get_def_team(
    college_team: Optional[str], player_name: Optional[str]
) -> Optional[str]:
    """
    Compute canonical DST team name, suitable to match CFBD roster 'team'.

    - First try Yahoo/Fantrax 'college_team' code via TEAM_CODE_TO_NAME.
    - Then try DST label via DST_LABEL_TO_NAME on player_name.
    """
    code = (college_team or "").upper()
    if code in TEAM_CODE_TO_NAME:
        return TEAM_CODE_TO_NAME[code]

    label = (player_name or "").strip()
    if label in DST_LABEL_TO_NAME:
        return DST_LABEL_TO_NAME[label]

    return None
