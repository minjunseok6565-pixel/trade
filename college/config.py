from __future__ import annotations

"""
College subsystem configuration.

All constants here are designed to be tuning knobs.
Keep them pure (no randomness at import time).
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple

# ----------------------------
# League shape
# ----------------------------

COLLEGE_TEAM_COUNT: int = 200
COLLEGE_ROSTER_SIZE: int = 15  # per team (scholarship-like)
COLLEGE_SEASON_GAMES_PER_TEAM: int = 34

# Team minutes per game in a 40-min NCAA-style game.
COLLEGE_TEAM_MINUTES_PER_GAME: int = 200  # 40 * 5

# Freshmen generated each offseason per team.
#
# We keep two distributions:
#  - BOOTSTRAP: used only at game start to generate 1~4 class years
#  - TARGET: used at offseason end (deficit-fill) as the desired roster shape
#
# NOTE: Offseason roster policy (freshmen + top-up) is controlled by OFFSEASON_* below.
BOOTSTRAP_CLASS_YEAR_COUNTS_PER_TEAM: Dict[int, int] = {
    1: 3,  # freshmen
    2: 4,  # soph
    3: 4,  # junior
    4: 4,  # senior
}
assert sum(BOOTSTRAP_CLASS_YEAR_COUNTS_PER_TEAM.values()) == COLLEGE_ROSTER_SIZE, (
    "bootstrap class-year distribution must match roster size"
)

TARGET_CLASS_YEAR_COUNTS_PER_TEAM: Dict[int, int] = {
    1: 3,
    2: 4,
    3: 4,
    4: 4,
}
assert sum(TARGET_CLASS_YEAR_COUNTS_PER_TEAM.values()) == COLLEGE_ROSTER_SIZE, (
    "target class-year distribution must match roster size"
)

# Backwards-compat alias used by older generation helpers.
CLASS_YEAR_COUNTS_PER_TEAM: Dict[int, int] = dict(BOOTSTRAP_CLASS_YEAR_COUNTS_PER_TEAM)

# ----------------------------
# Offseason roster policy
# ----------------------------

# Always add this many freshmen (class_year=1) each offseason per team.
OFFSEASON_FRESHMEN_PER_TEAM: int = 4

# After adding freshmen, if ACTIVE roster is still below this threshold,
# top-up from 2nd/3rd year (priority: 2 > 3) to reach this minimum.
OFFSEASON_MIN_ROSTER: int = 14

# Hard cap after offseason actions (trim lowest OVR if exceeded).
OFFSEASON_HARD_CAP: int = COLLEGE_ROSTER_SIZE

# Legacy / testing knob (kept for backwards compatibility).
FRESHMEN_PER_TEAM_PER_YEAR: int = OFFSEASON_FRESHMEN_PER_TEAM

# Draft eligibility baseline
MIN_DRAFT_ELIGIBLE_AGE: int = 19

# ----------------------------
# Talent / draft class strength
# ----------------------------

# class_strength is stored per draft_year as a scalar.
# -2.0 = very weak, +2.0 = legendary
CLASS_STRENGTH_CLAMP: Tuple[float, float] = (-2.0, 2.0)

# Sampling for class_strength if not present in DB yet.
# N(0, CLASS_STRENGTH_STD) clipped to clamp.
CLASS_STRENGTH_STD: float = 0.85

# ----------------------------
# Player generation knobs
# ----------------------------

# Typical NCAA size ranges (tunable)
HEIGHT_IN_RANGE = (70, 86)  # 5'10" to 7'2"
WEIGHT_LB_RANGE = (160, 285)

# Position weights
POS_WEIGHTS: Dict[str, float] = {
    "PG": 0.20,
    "SG": 0.22,
    "SF": 0.22,
    "PF": 0.20,
    "C": 0.16,
}

# Age distribution by class year (base age when entering that class year)
# We sample small jitter around these.
BASE_AGE_BY_CLASS_YEAR: Dict[int, int] = {1: 18, 2: 19, 3: 20, 4: 21}

# Overall rating bounds for college players (tunable)
COLLEGE_OVR_RANGE = (40, 86)  # college pool contains some real prospects, but fewer NBA-ready 90+

# ----------------------------
# Stat simulation knobs (lightweight season sim)
# ----------------------------

# Rough NCAA-ish team PPG distribution center
TEAM_PPG_MEAN: float = 72.0
TEAM_PPG_STD: float = 6.5

# Pace-ish knob used internally to shape per-player counting stats
POSSESSIONS_MEAN: float = 68.0
POSSESSIONS_STD: float = 4.0

# Shooting splits anchors
FG_PCT_MEAN: float = 0.45
FG_PCT_STD: float = 0.03
TP_PCT_MEAN: float = 0.34
TP_PCT_STD: float = 0.04
FT_PCT_MEAN: float = 0.73
FT_PCT_STD: float = 0.06

# ----------------------------
# Fictional team list (avoid licensing issues)
# ----------------------------

COLLEGE_CONFERENCES: List[str] = [
    "North",
    "South",
    "East",
    "West",
]

@dataclass(frozen=True, slots=True)
class CollegeTeamSeed:
    college_team_id: str
    name: str
    conference: str


# 32 teams across 4 conferences (fictional but plausible)
COLLEGE_TEAMS: List[CollegeTeamSeed] = [
    # North
    CollegeTeamSeed("COL_001", "Great Lakes State", "North"),
    CollegeTeamSeed("COL_002", "Ironwood University", "North"),
    CollegeTeamSeed("COL_003", "Pine Ridge Tech", "North"),
    CollegeTeamSeed("COL_004", "Cedar Valley", "North"),
    CollegeTeamSeed("COL_005", "Metro Northern", "North"),
    CollegeTeamSeed("COL_006", "Summit City", "North"),
    CollegeTeamSeed("COL_007", "Blue Harbor", "North"),
    CollegeTeamSeed("COL_008", "Stonemill College", "North"),
    # South
    CollegeTeamSeed("COL_009", "Coastal Carolina City", "South"),
    CollegeTeamSeed("COL_010", "Bayou State", "South"),
    CollegeTeamSeed("COL_011", "Sunbelt University", "South"),
    CollegeTeamSeed("COL_012", "Desert Ridge", "South"),
    CollegeTeamSeed("COL_013", "Gulfshore Tech", "South"),
    CollegeTeamSeed("COL_014", "Magnolia A&M", "South"),
    CollegeTeamSeed("COL_015", "Riverbend", "South"),
    CollegeTeamSeed("COL_016", "Capitol South", "South"),
    # East
    CollegeTeamSeed("COL_017", "Atlantic Heights", "East"),
    CollegeTeamSeed("COL_018", "Stonebridge", "East"),
    CollegeTeamSeed("COL_019", "Harborview", "East"),
    CollegeTeamSeed("COL_020", "Kingsport", "East"),
    CollegeTeamSeed("COL_021", "Crown City", "East"),
    CollegeTeamSeed("COL_022", "Palisade Institute", "East"),
    CollegeTeamSeed("COL_023", "Union Metropolitan", "East"),
    CollegeTeamSeed("COL_024", "Fairmont", "East"),
    # West
    CollegeTeamSeed("COL_025", "Pacific Crest", "West"),
    CollegeTeamSeed("COL_026", "Redstone Tech", "West"),
    CollegeTeamSeed("COL_027", "Sierra State", "West"),
    CollegeTeamSeed("COL_028", "Canyon University", "West"),
    CollegeTeamSeed("COL_029", "Golden Bay", "West"),
    CollegeTeamSeed("COL_030", "Frontier College", "West"),
    CollegeTeamSeed("COL_031", "Highland Poly", "West"),
    CollegeTeamSeed("COL_032", "Sequoia University", "West"),
]
assert len(COLLEGE_TEAMS) <= COLLEGE_TEAM_COUNT, "COLLEGE_TEAMS must not exceed COLLEGE_TEAM_COUNT"
