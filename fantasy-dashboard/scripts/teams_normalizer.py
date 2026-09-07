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
    "USD": "San Diego",  # verify - FCS, may be data noise (only 10 rows)
    "UTAH": "Utah",
    "UVA": "Virginia",
    "UW": "Washington",
    "VAN": "Vanderbilt",
    "VT": "Virginia Tech",
    "WAKE": "Wake Forest",
    "WIS": "Wisconsin",
    "WVU": "West Virginia",
    # Previously known but not in this sample; keep for safety
    "ALA": "Alabama",
    "AF": "Air Force",
    "ARK": "Arkansas",
    "ARKST": "Arkansas State",
}

# Fantrax DST labels / shorthand names → CFBD roster team names
DST_LABEL_TO_NAME = {
    "JacSt": "Jacksonville State",
    "JMU": "James Madison",
    "K St": "Kansas State",
    "KY": "Kentucky",
    "Lou": "Louisville",
    "LSU": "LSU",
    "Marsh": "Marshall",
    "MD": "Maryland",
    "Mem": "Memphis",
    "MiaFL": "Miami",
    "MiaOH": "Miami (OH)",
    "Mich": "Michigan",
    "MIN": "Minnesota",
    "Miss": "Ole Miss",
    "Mizzou": "Missouri",
    "Navy": "Navy",
    "NCSt": "NC State",
    "ND": "Notre Dame",
    "NDSU": "North Dakota State",
    "Neb": "Nebraska",
    "NIU": "Northern Illinois",
    "NM": "New Mexico",
    "Okla": "Oklahoma",
    "Oreg": "Oregon",
    "OSU": "Ohio State",
    "Pitt": "Pittsburgh",
    "PSU": "Penn State",
    "Rut": "Rutgers",
    "SacSt": "Sacramento State",
    "SCar": "South Carolina",
    "SDSU": "San Diego State",
    "SMU": "SMU",
    "SoFL": "South Florida",
    "Tenn": "Tennessee",
    "Tex": "Texas",
    "Toled": "Toledo",
    "Troy": "Troy",
    "Tul": "Tulane",
    "TxAM": "Texas A&M",
    "TxTch": "Texas Tech",
    "UCF": "UCF",
    "UConn": "UConn",
    "UGA": "Georgia",
    "USC": "USC",
    "Utah": "Utah",
    "UTSA": "UTSA",
    "UtSt": "Utah State",
    "UVA": "Virginia",
    "VaTec": "Virginia Tech",
    "Wash": "Washington",
    "WestMI": "Western Michigan",
    "Wisc": "Wisconsin",
    "WVU": "West Virginia",
    # Previously known
    "AF": "Air Force",
    "Ark": "Arkansas",
    "ArkSt": "Arkansas State",
    "Aub": "Auburn",
    "Bama": "Alabama",
    "Boise": "Boise State",
    "Buff": "Buffalo",
    "BYU": "BYU",
    "Clem": "Clemson",
    "CoCar": "Coastal Carolina",
    "C Mi": "Central Michigan",
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
