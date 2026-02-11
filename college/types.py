from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

JsonDict = Dict[str, Any]


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def json_loads(s: str) -> Any:
    return json.loads(s) if s else None


@dataclass(frozen=True, slots=True)
class CollegeTeam:
    college_team_id: str
    name: str
    conference: str
    meta: JsonDict


@dataclass(frozen=True, slots=True)
class CollegePlayer:
    """
    College-only player record (separate from NBA players/roster until drafted).

    player_id is allocated in the same namespace as NBA player_id so that
    drafting can "promote" the same player_id into players/roster without remap.
    """
    player_id: str
    name: str
    pos: str
    age: int
    height_in: int
    weight_lb: int
    ovr: int

    college_team_id: str
    class_year: int  # 1..4
    entry_season_year: int
    status: str  # ACTIVE / DECLARED / DRAFTED / GRADUATED

    attrs: JsonDict  # includes at least: potential (int)


@dataclass(frozen=True, slots=True)
class CollegeSeasonStats:
    """
    Per-season aggregated stats. This is enough for a scouting UI.
    We store both per-game and totals (derived), to avoid UI recalcs.
    """
    season_year: int
    player_id: str
    college_team_id: str

    games: int
    mpg: float

    pts: float
    reb: float
    ast: float
    stl: float
    blk: float
    tov: float
    pf: float

    fg_pct: float
    tp_pct: float
    ft_pct: float

    # Optional flavor metrics (kept simple)
    usg: float          # 0..1
    ts_pct: float       # 0..1
    pace: float         # possessions per game proxy

    meta: JsonDict

    def to_json_dict(self) -> JsonDict:
        """Stable JSON payload for DB storage (slots-safe; no __dict__ dependency)."""
        from .serialization import season_stats_to_json
        return season_stats_to_json(self)


@dataclass(frozen=True, slots=True)
class CollegeTeamSeasonStats:
    season_year: int
    college_team_id: str
    wins: int
    losses: int
    srs: float
    pace: float
    off_ppg: float
    def_ppg: float
    meta: JsonDict


@dataclass(frozen=True, slots=True)
class DraftEntryDecisionTrace:
    """
    Trace for why a player declared.

    Store this JSON in DB for tuning & debugging (commercial-quality telemetry).
    """
    player_id: str
    draft_year: int
    declared: bool
    declare_prob: float
    projected_pick: Optional[int]
    factors: JsonDict  # component scores (ovr, age, class, production, risk, etc.)
    notes: JsonDict    # any extra notes (thresholds, clamps, etc.)

    def to_json_dict(self) -> JsonDict:
        """Stable JSON payload for DB storage (slots-safe; no __dict__ dependency)."""
        from .serialization import decision_trace_to_json
        return decision_trace_to_json(self)
