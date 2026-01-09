import os
from typing import Any, Dict, List, Optional

import math
import os
from typing import Any, Dict, List, Optional

import pandas as pd

# -------------------------------------------------------------------------
# 0. 기본 설정 / 로스터 로딩
# -------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROSTER_PATH = os.path.join(BASE_DIR, "완성 로스터.xlsx")

if not os.path.exists(ROSTER_PATH):
    raise RuntimeError(f"로스터 엑셀 파일을 찾을 수 없습니다: {ROSTER_PATH}")

ROSTER_DF = pd.read_excel(ROSTER_PATH)

# Salary 문자열을 숫자(달러)로 변환
def _parse_salary(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return 0.0
        return float(value)
    s = str(value).strip()
    if not s or s == "--":
        return 0.0
    # "$15,161,800" 같은 형식 처리
    for ch in ["$", ","]:
        s = s.replace(ch, "")
    try:
        return float(s)
    except ValueError:
        return 0.0


# 숫자 샐러리 컬럼을 하나 추가해두면 이후 계산이 편하다.
if "Salary" in ROSTER_DF.columns:
    ROSTER_DF["SalaryAmount"] = ROSTER_DF["Salary"].apply(_parse_salary)
else:
    ROSTER_DF["SalaryAmount"] = 0.0

# 리그/디비전 설정 (프론트 script.js 의 DIVISIONS와 동일하게 맞춤)
ALL_TEAM_IDS: List[str] = sorted(
    {
        str(team).strip()
        for team in ROSTER_DF["Team"].unique()
        if team
        and str(team).strip()
        and str(team).strip().upper() != "FA"
    }
)

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
