from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, Optional

from schema import season_id_from_year as _schema_season_id_from_year
from state_cache import _reset_cached_views_for_new_season
from state_store import DEFAULT_TRADE_RULES, GAME_STATE
from state_trade import _ensure_trade_state


def ensure_league_block() -> Dict[str, Any]:
    """Ensure the minimal in-memory league scaffold exists.

    This is intentionally *in-memory only*: no DB init, no roster seeding, no integrity checks.
    """
    league = GAME_STATE.setdefault("league", {})
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


def get_current_date() -> Optional[str]:
    """Return the league's current in-game date (SSOT: GAME_STATE['league']['current_date'])."""
    league = ensure_league_block()
    current = league.get("current_date")
    if current:
        return current
    return None


def get_current_date_as_date() -> date:
    """Return the league's current in-game date as a date object."""
    current = get_current_date()
    if current:
        try:
            return date.fromisoformat(str(current))
        except ValueError:
            pass

    league = ensure_league_block()
    season_start = league.get("season_start")
    if season_start:
        try:
            return date.fromisoformat(str(season_start))
        except ValueError:
            pass

    return date.today()


def set_current_date(date_str: Optional[str]) -> None:
    """Update the league's current date (SSOT: GAME_STATE['league']['current_date'])."""
    league = ensure_league_block()
    league["current_date"] = date_str


def _season_id_from_year(season_year: int) -> str:
    """시즌 시작 연도(int) -> season_id 문자열로 변환. 예: 2025 -> '2025-26'"""
    return str(_schema_season_id_from_year(int(season_year)))


def _archive_and_reset_season_accumulators(
    previous_season_id: Optional[str],
    next_season_id: Optional[str],
) -> None:
    """시즌이 바뀔 때 정규시즌 누적 데이터를 history로 옮기고 초기화한다."""
    if previous_season_id:
        history = GAME_STATE.setdefault("season_history", {})
        history[str(previous_season_id)] = {
            "games": GAME_STATE.get("games", []),
            "player_stats": GAME_STATE.get("player_stats", {}),
            "team_stats": GAME_STATE.get("team_stats", {}),
            "game_results": GAME_STATE.get("game_results", {}),
        }

    GAME_STATE["games"] = []
    GAME_STATE["player_stats"] = {}
    GAME_STATE["team_stats"] = {}
    GAME_STATE["game_results"] = {}
    GAME_STATE["postseason"] = {}

    GAME_STATE["active_season_id"] = next_season_id
    _reset_cached_views_for_new_season()
    _ensure_trade_state()


def _ensure_active_season_id(season_id: str) -> None:
    """리그 시즌과 누적 시즌이 불일치하면 새 시즌 누적으로 전환한다."""
    if not season_id:
        return
    active = GAME_STATE.get("active_season_id")
    if active is None:
        GAME_STATE["active_season_id"] = str(season_id)
        _ensure_trade_state()
        return
    if str(active) != str(season_id):
        _archive_and_reset_season_accumulators(str(active), str(season_id))


def _get_phase_container(phase: str) -> Dict[str, Any]:
    """phase별 누적 컨테이너를 반환한다."""
    if phase == "regular":
        return GAME_STATE
    postseason = GAME_STATE.setdefault("postseason", {})
    return postseason.setdefault(phase, {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}})
