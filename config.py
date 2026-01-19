import os
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class _LegacyRosterDF:
    """Legacy roster DataFrame placeholder.

    NOTE: Runtime code must use LeagueRepo/SQLite. This sentinel prevents any
    silent fallback to Excel-backed DataFrame usage.
    """

    def __getattr__(self, name: str) -> None:
        raise RuntimeError("ROSTER_DF is removed; use LeagueRepo/SQLite instead.")

    def __getitem__(self, key: object) -> None:
        raise RuntimeError("ROSTER_DF is removed; use LeagueRepo/SQLite instead.")

    def __setitem__(self, key: object, value: object) -> None:
        raise RuntimeError("ROSTER_DF is removed; use LeagueRepo/SQLite instead.")

    def __bool__(self) -> bool:
        raise RuntimeError("ROSTER_DF is removed; use LeagueRepo/SQLite instead.")

    def __repr__(self) -> str:
        return "<LegacyRosterDF disabled; use LeagueRepo/SQLite instead>"


ROSTER_DF = _LegacyRosterDF()

# 리그/디비전 설정 (프론트 script.js 의 DIVISIONS와 동일하게 맞춤)
DIVISIONS: Dict[str, Dict[str, List[str]]] = {
    "West": {
        "Southwest": ["DAL", "HOU", "MEM", "NOP", "SAS"],
        "Northwest": ["DEN", "MIN", "OKC", "POR", "UTA"],
        "Pacific": ["GSW", "LAC", "LAL", "PHX", "SAC"],
    },
    "East": {
        "Atlantic": ["BOS", "BKN", "NYK", "PHI", "TOR"],
        "Central": ["CHI", "CLE", "DET", "IND", "MIL"],
        "Southeast": ["ATL", "CHA", "MIA", "ORL", "WAS"],
    },
}

ALL_TEAM_IDS: List[str] = sorted(
    {team_id for divs in DIVISIONS.values() for teams in divs.values() for team_id in teams}
)

TEAM_TO_CONF_DIV: Dict[str, Dict[str, Optional[str]]] = {}
for conf, divs in DIVISIONS.items():
    for div_name, team_ids in divs.items():
        for tid in team_ids:
            TEAM_TO_CONF_DIV[tid] = {"conference": conf, "division": div_name}

# 디비전 매핑에 없는 팀이 있다면 경고만 출력 (실행은 계속 진행)
MISSING_DIVISION_TEAMS = [t for t in ALL_TEAM_IDS if t not in TEAM_TO_CONF_DIV]
if MISSING_DIVISION_TEAMS:
    print("[WARN] DIVISIONS 매핑에 없는 팀:", MISSING_DIVISION_TEAMS)

# 시즌 기본 설정
SEASON_START_MONTH = 10  # 10월
SEASON_START_DAY = 19
SEASON_LENGTH_DAYS = 180  # 대략 6개월
MAX_GAMES_PER_DAY = 8

# 시즌/샐러리캡 기본 설정
INITIAL_SEASON_YEAR = 2025
CAP_BASE_SEASON_YEAR = 2025
CAP_BASE_SALARY_CAP = 154_647_000
CAP_BASE_FIRST_APRON = 195_945_000
CAP_BASE_SECOND_APRON = 207_824_000
CAP_ANNUAL_GROWTH_RATE = 0.10  # 10% per season
CAP_ROUND_UNIT = 1000  # round to nearest $1,000
