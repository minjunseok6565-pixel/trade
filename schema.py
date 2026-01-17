# schema.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, NewType, Optional, Sequence, Tuple, TypedDict, Literal
import re
import uuid

# ============================================================================
# 0) Single Source of Truth: IDs / Versions
# ============================================================================

SCHEMA_VERSION: str = "2.0"

# IMPORTANT:
# - Always treat IDs as str. Never use int keys for players/teams.
PlayerId = NewType("PlayerId", str)
TeamId = NewType("TeamId", str)
SeasonId = NewType("SeasonId", str)
GameId = NewType("GameId", str)

Phase = Literal["regular", "play_in", "playoffs", "preseason"]

ALLOWED_PHASES: Tuple[Phase, ...] = ("regular", "play_in", "playoffs", "preseason")

# PlayerID canonical format recommendation:
#   P000001, P000002, ...
# (Human-friendly, sortable, avoids int/str split.)
PLAYER_ID_RE = re.compile(r"^P\d{6}$")

# TeamID canonical format recommendation:
#   3-letter uppercase NBA code, plus "FA".
TEAM_ID_RE = re.compile(r"^(?:[A-Z]{3}|FA)$")


def is_canonical_player_id(pid: str) -> bool:
    return bool(PLAYER_ID_RE.match(pid))


def is_canonical_team_id(tid: str) -> bool:
    return bool(TEAM_ID_RE.match(tid))

# Engine raw often uses these side keys for home/away mapping.
SIDE_HOME: str = "home"
SIDE_AWAY: str = "away"
RAW_SIDE_KEYS: Tuple[str, str] = (SIDE_HOME, SIDE_AWAY)


# ============================================================================
# 1) Roster / Data Columns (Excel / DataFrame)
# ============================================================================

# Canonical columns we want going forward.
ROSTER_COL_PLAYER_ID = "player_id"   # REQUIRED long-term
ROSTER_COL_TEAM_ID = "team_id"       # REQUIRED long-term
ROSTER_COL_NAME = "name"
ROSTER_COL_POS = "pos"
ROSTER_COL_AGE = "age"
ROSTER_COL_HEIGHT_IN = "height_in"
ROSTER_COL_WEIGHT_LB = "weight_lb"
ROSTER_COL_SALARY_AMOUNT = "salary_amount"
ROSTER_COL_OVR = "ovr"

# If you keep roster as a DataFrame, we strongly recommend:
# - do NOT use DataFrame index as player_id.
# - always use ROSTER_COL_PLAYER_ID column as the only player key.


# ============================================================================
# 2) Boxscore / Stats Key Standards
# ============================================================================

# Player boxscore required identifiers
BOX_PLAYER_ID = "PlayerID"
BOX_TEAM_ID = "TeamID"
BOX_NAME = "Name"
BOX_MIN = "MIN"

# Countable stats (safe to sum across games)
# (Derived % should NOT live at top-level, or state.py will accidentally sum them.)
STAT_PTS = "PTS"
STAT_FGM = "FGM"
STAT_FGA = "FGA"
STAT_3PM = "3PM"
STAT_3PA = "3PA"
STAT_FTM = "FTM"
STAT_FTA = "FTA"
STAT_ORB = "ORB"
STAT_DRB = "DRB"
STAT_REB = "REB"
STAT_AST = "AST"
STAT_TOV = "TOV"
STAT_PF = "PF"

CANONICAL_PLAYER_COUNT_STATS: Tuple[str, ...] = (
    STAT_PTS, STAT_FGM, STAT_FGA, STAT_3PM, STAT_3PA, STAT_FTM, STAT_FTA,
    STAT_ORB, STAT_DRB, STAT_REB, STAT_AST, STAT_TOV, STAT_PF
)

# Team totals (safe to sum across games/quarters/possessions)
TEAM_POSSESSIONS = "Possessions"
TEAM_PITP = "PITP"
TEAM_FASTBREAK_PTS = "FastbreakPTS"
TEAM_SECOND_CHANCE_PTS = "SecondChancePTS"
TEAM_POINTS_OFF_TOV = "PointsOffTOV"

CANONICAL_TEAM_TOTALS: Tuple[str, ...] = (
    STAT_PTS, STAT_FGM, STAT_FGA, STAT_3PM, STAT_3PA, STAT_FTM, STAT_FTA,
    STAT_TOV, STAT_ORB, STAT_DRB,
    TEAM_POSSESSIONS, STAT_AST,
    TEAM_PITP, TEAM_FASTBREAK_PTS, TEAM_SECOND_CHANCE_PTS, TEAM_POINTS_OFF_TOV,
)

# Breakdown dict keys that match your current engine output conventions.
BREAKDOWN_KEYS: Tuple[str, ...] = (
    "PossessionEndCounts",
    "ShotZoneDetail",
    "OffActionCounts",
    "DefActionCounts",
    "OutcomeCounts",
    "ShotZones",
)

# Percentages should be nested under a single field so they never get "summed" by state.py.
DERIVED = "derived"
DER_FG_PCT = "FG_PCT"
DER_3P_PCT = "3P_PCT"
DER_FT_PCT = "FT_PCT"

RAW_FG_PCT = "FG%"
RAW_3P_PCT = "3P%"
RAW_FT_PCT = "FT%"

RAW_PCT_KEYS: Tuple[str, ...] = (RAW_FG_PCT, RAW_3P_PCT, RAW_FT_PCT)


# ============================================================================
# 3) GameResultV2 (what state.py ingests) - Types
# ============================================================================

class V2Game(TypedDict):
    game_id: str
    date: str                  # ISO date: "YYYY-MM-DD"
    season_id: str             # ex: "2025-26"
    phase: Phase
    home_team_id: str
    away_team_id: str
    overtime_periods: int
    possessions_per_team: int


class V2TeamResult(TypedDict, total=False):
    team_id: str
    totals: Dict[str, Any]
    breakdowns: Dict[str, Any]
    players: List[Dict[str, Any]]
    extra_totals: Dict[str, Any]
    extra_breakdowns: Dict[str, Any]


class V2GameState(TypedDict):
    team_fouls: Dict[str, Any]          # {team_id: int}
    player_fouls: Dict[str, Any]        # {team_id: {player_id: int}}
    fatigue: Dict[str, Any]             # {team_id: {player_id: float}}
    minutes_played_sec: Dict[str, Any]  # {team_id: {player_id: int}}


class V2Meta(TypedDict):
    engine_name: str
    engine_version: str
    era: str
    era_version: str
    replay_token: str


class GameResultV2(TypedDict, total=False):
    schema_version: str
    game: V2Game
    final: Dict[str, int]          # {team_id: points}
    teams: Dict[str, V2TeamResult] # {team_id: V2TeamResult}
    game_state: V2GameState
    meta: V2Meta
    debug: Dict[str, Any]
    raw: Dict[str, Any]


# ============================================================================
# 4) Normalization / Validation Utilities (must be used everywhere)
# ============================================================================

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def normalize_team_id(value: Any, *, allow_fa: bool = True, strict: bool = True) -> TeamId:
    """
    Normalize team id into canonical form.
    - trims spaces
    - uppercases
    - enforces 3-letter code or 'FA' (recommended)
    """
    s = str(value).strip().upper()
    if not s:
        raise ValueError("team_id is empty")

    if allow_fa and s == "FA":
        return TeamId("FA")

    if strict:
        if not TEAM_ID_RE.match(s):
            raise ValueError(f"invalid team_id '{s}' (expected 3 uppercase letters or 'FA')")
    return TeamId(s)

def normalize_player_id(
    value: Any,
    *,
    strict: bool = True,
    allow_legacy_numeric: bool = False,
) -> PlayerId:
    """
    Normalize player id into canonical form.

    Recommended canonical format: P000001.
    - strict=True enforces 'P' + 6 digits.
    - allow_legacy_numeric=True allows "12" style IDs (NOT recommended long-term).
      If allowed, it returns the string as-is (still str), so you avoid int/str split.
    """
    s = str(value).strip()
    if not s:
        raise ValueError("player_id is empty")

    if PLAYER_ID_RE.match(s):
        return PlayerId(s)

    if allow_legacy_numeric and s.isdigit():
        # Legacy mode: keep as str, never int.
        return PlayerId(s)

    if strict:
        raise ValueError(f"invalid player_id '{s}' (expected like P000001)")
    return PlayerId(s)

def season_id_from_year(season_year: int) -> SeasonId:
    """Example: 2025 -> '2025-26'"""
    yy = str(int(season_year) + 1)[-2:]
    return SeasonId(f"{int(season_year)}-{yy}")

def make_player_id_seq(n: int) -> PlayerId:
    """Helper for migrations: 1 -> P000001"""
    return PlayerId(f"P{int(n):06d}")

def make_player_id_uuid() -> PlayerId:
    """Alternative migration strategy (very stable, less human-friendly)."""
    # Example: P_2f1c1f...  (If you use this, relax PLAYER_ID_RE and adjust strict checks.)
    return PlayerId("P" + uuid.uuid4().hex[:10].upper())

def assert_unique_ids(player_ids: Iterable[str], *, what: str = "player_id") -> None:
    seen: set[str] = set()
    dups: set[str] = set()
    for x in player_ids:
        if x in seen:
            dups.add(x)
        seen.add(x)
    if dups:
        raise ValueError(f"duplicate {what}: {sorted(dups)!r}")

def assert_game_result_v2_minimum_shape(result: Mapping[str, Any]) -> None:
    """
    Minimal structural checks that should match state.py validator expectations.
    This is intentionally strict: if it fails, fix upstream rather than guessing.
    """
    if not isinstance(result, Mapping):
        raise ValueError("GameResultV2 must be a dict-like mapping")

    if result.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"GameResultV2.schema_version must be '{SCHEMA_VERSION}'")

    game = result.get("game")
    if not isinstance(game, Mapping):
        raise ValueError("GameResultV2.game must be a dict")
    for k in ("game_id", "date", "season_id", "phase", "home_team_id", "away_team_id", "overtime_periods", "possessions_per_team"):
        if k not in game:
            raise ValueError(f"GameResultV2.game missing '{k}'")

    phase = str(game.get("phase"))
    if phase not in ALLOWED_PHASES:
        raise ValueError(f"GameResultV2.game.phase must be one of {ALLOWED_PHASES}, got '{phase}'")

    home_id = normalize_team_id(game.get("home_team_id"), strict=False)
    away_id = normalize_team_id(game.get("away_team_id"), strict=False)

    final = result.get("final")
    if not isinstance(final, Mapping) or str(home_id) not in final or str(away_id) not in final:
        raise ValueError("GameResultV2.final must include both home and away team ids")

    teams = result.get("teams")
    if not isinstance(teams, Mapping) or str(home_id) not in teams or str(away_id) not in teams:
        raise ValueError("GameResultV2.teams must include both home and away team ids")

    for tid in (str(home_id), str(away_id)):
        team_obj = teams.get(tid)
        if not isinstance(team_obj, Mapping):
            raise ValueError(f"GameResultV2.teams['{tid}'] must be a dict")
        totals = team_obj.get("totals")
        if not isinstance(totals, Mapping) or STAT_PTS not in totals:
            raise ValueError(f"GameResultV2.teams['{tid}'].totals must include '{STAT_PTS}'")
        players = team_obj.get("players")
        if not isinstance(players, list):
            raise ValueError(f"GameResultV2.teams['{tid}'].players must be a list")
        for i, row in enumerate(players):
            if not isinstance(row, Mapping):
                raise ValueError(f"teams['{tid}'].players[{i}] must be a dict")
            if BOX_PLAYER_ID not in row or BOX_TEAM_ID not in row:
                raise ValueError(f"teams['{tid}'].players[{i}] must include PlayerID and TeamID")
            if str(row[BOX_TEAM_ID]) != tid:
                raise ValueError(f"teams['{tid}'].players[{i}].TeamID must match '{tid}'")

def normalize_side_keyed_dict_to_team_ids(
    obj: Any,
    *,
    home_team_id: TeamId,
    away_team_id: TeamId,
    path: str,
) -> Dict[str, Any]:
    """
    Convert {'home': X, 'away': Y} -> {home_team_id: X, away_team_id: Y}.
    If already keyed by team ids, pass through.
    Otherwise raise ValueError (no guessing).
    """
    hid, aid = str(home_team_id), str(away_team_id)

    if obj is None:
        return {hid: {}, aid: {}}
    if not isinstance(obj, dict):
        raise ValueError(f"'{path}' must be a dict")

    if SIDE_HOME in obj and SIDE_AWAY in obj:
        return {hid: obj.get(SIDE_HOME, {}), aid: obj.get(SIDE_AWAY, {})}

    if hid in obj and aid in obj:
        return {hid: obj.get(hid, {}), aid: obj.get(aid, {})}

    raise ValueError(
        f"cannot map '{path}' keys to team ids; got keys={list(obj.keys())!r}, "
        f"expected either ['home','away'] or team ids ['{hid}','{aid}']"
    )

def normalize_player_keyed_map(
    obj: Any,
    *,
    strict_player_id: bool = True,
    allow_legacy_numeric: bool = False,
) -> Dict[str, Any]:
    """
    Normalize dict keys into canonical PlayerID strings.
    Example: {12: 3} -> {"P000012": 3} (if you later choose to convert),
    but by default we enforce canonical 'P000001' format.

    NOTE: This function does not know how to convert numeric -> P000xxx safely
    unless you've already adopted canonical IDs. So default behavior is strict.
    """
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise ValueError("expected dict keyed by player_id")
    out: Dict[str, Any] = {}
    for k, v in obj.items():
        pid = normalize_player_id(k, strict=strict_player_id, allow_legacy_numeric=allow_legacy_numeric)
        out[str(pid)] = v
    return out


# ============================================================================
# 5) Recommended “No-footgun” rules (documented as constants)
# ============================================================================

# Any module that needs a player identifier should use:
# - player_id (str) in canonical format (recommended P000001)
# - NEVER DataFrame index
# - NEVER int

# Any module that produces game results should produce GameResultV2 as defined above.
# matchengine_v2_adapter should be the only boundary module that knows about raw engine quirks.
