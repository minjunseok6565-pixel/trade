from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, Optional

from schema import season_id_from_year as _schema_season_id_from_year
from .state_cache import _reset_cached_views_for_new_season
from .state_constants import DEFAULT_TRADE_RULES
from .state_trade import _ensure_trade_state


def ensure_league_block(state: dict) -> Dict[str, Any]:
    """Ensure the minimal in-memory league scaffold exists.

    This is intentionally *in-memory only*: no DB init, no roster seeding, no integrity checks.
    """
    league = state.setdefault("league", {})
    master_schedule = league.setdefault("master_schedule", {})
    master_schedule.setdefault("games", [])
    master_schedule.setdefault("by_team", {})
    master_schedule.setdefault("by_date", {})
    master_schedule.setdefault("by_id", {})

    trade_rules = league.setdefault("trade_rules", {})
    if not isinstance(trade_rules, dict):
        trade_rules = {}
        league["trade_rules"] = trade_rules
    for key, value in DEFAULT_TRADE_RULES.items():
        trade_rules.setdefault(key, value)

    league.setdefault("season_year", None)
    league.setdefault("draft_year", None)
    league.setdefault("season_start", None)
    league.setdefault("current_date", None)
    league.setdefault("last_gm_tick_date", None)

    db_path = league.get("db_path") or os.environ.get("LEAGUE_DB_PATH") or "league.db"
    league["db_path"] = db_path
    return league


def get_current_date(state: dict) -> Optional[str]:
    """Return the league's current in-game date (SSOT: state['league']['current_date'])."""
    league = ensure_league_block(state)
    current = league.get("current_date")
    if current:
        return current
    return None


def get_current_date_as_date(state: dict) -> date:
    """Return the league's current in-game date as a date object."""
    current = get_current_date(state)
    if current:
        try:
            return date.fromisoformat(str(current))
        except ValueError:
            pass

    league = ensure_league_block(state)
    season_start = league.get("season_start")
    if season_start:
        try:
            return date.fromisoformat(str(season_start))
        except ValueError:
            pass

    return date.today()


def set_current_date(state: dict, date_str: Optional[str]) -> None:
    """Update the league's current date (SSOT: state['league']['current_date'])."""
    league = ensure_league_block(state)
    league["current_date"] = date_str


def _season_id_from_year(season_year: int) -> str:
    """시즌 시작 연도(int) -> season_id 문자열로 변환. 예: 2025 -> '2025-26'"""
    return str(_schema_season_id_from_year(int(season_year)))


def _archive_and_reset_season_accumulators(
    state: dict,
    previous_season_id: Optional[str],
    next_season_id: Optional[str],
) -> None:
    """시즌이 바뀔 때 정규시즌 누적 데이터를 history로 옮기고 초기화한다."""
    if previous_season_id:
        history = state.setdefault("season_history", {})
        history[str(previous_season_id)] = {
            "regular": {
                "games": list(state.get("games", [])),
                "player_stats": dict(state.get("player_stats", {})),
                "team_stats": dict(state.get("team_stats", {})),
                "game_results": dict(state.get("game_results", {})),
            },
            "phase_results": {
                "preseason": {
                    "games": list(state.get("phase_results", {}).get("preseason", {}).get("games", [])),
                    "player_stats": dict(state.get("phase_results", {}).get("preseason", {}).get("player_stats", {})),
                    "team_stats": dict(state.get("phase_results", {}).get("preseason", {}).get("team_stats", {})),
                    "game_results": dict(state.get("phase_results", {}).get("preseason", {}).get("game_results", {})),
                },
                "play_in": {
                    "games": list(state.get("phase_results", {}).get("play_in", {}).get("games", [])),
                    "player_stats": dict(state.get("phase_results", {}).get("play_in", {}).get("player_stats", {})),
                    "team_stats": dict(state.get("phase_results", {}).get("play_in", {}).get("team_stats", {})),
                    "game_results": dict(state.get("phase_results", {}).get("play_in", {}).get("game_results", {})),
                },
                "playoffs": {
                    "games": list(state.get("phase_results", {}).get("playoffs", {}).get("games", [])),
                    "player_stats": dict(state.get("phase_results", {}).get("playoffs", {}).get("player_stats", {})),
                    "team_stats": dict(state.get("phase_results", {}).get("playoffs", {}).get("team_stats", {})),
                    "game_results": dict(state.get("phase_results", {}).get("playoffs", {}).get("game_results", {})),
                },
            },
        }

    state["games"] = []
    state["player_stats"] = {}
    state["team_stats"] = {}
    state["game_results"] = {}
    state["phase_results"] = {
        "preseason": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
        "play_in": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
        "playoffs": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
    }

    state["active_season_id"] = next_season_id
    _reset_cached_views_for_new_season(state)
    _ensure_trade_state(state)
    from state_schema import validate_game_state
    validate_game_state(state)


def _ensure_active_season_id(state: dict, season_id: str) -> None:
    """리그 시즌과 누적 시즌이 불일치하면 새 시즌 누적으로 전환한다."""
    if not season_id:
        return
    active = state.get("active_season_id")
    if active is None:
        state["active_season_id"] = str(season_id)
        _ensure_trade_state(state)
        return
    if str(active) != str(season_id):
        _archive_and_reset_season_accumulators(state, str(active), str(season_id))


def _get_phase_container(state: dict, phase: str) -> Dict[str, Any]:
    """phase별 누적 컨테이너를 반환한다."""
    if phase == "regular":
        return state
    phase_results = state.setdefault("phase_results", {})
    return phase_results.setdefault(phase, {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}})
