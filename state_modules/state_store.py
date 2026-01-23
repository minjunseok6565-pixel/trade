from __future__ import annotations

from typing import Any, Dict

from config import (
    CAP_ANNUAL_GROWTH_RATE,
    CAP_BASE_FIRST_APRON,
    CAP_BASE_SALARY_CAP,
    CAP_BASE_SECOND_APRON,
    CAP_BASE_SEASON_YEAR,
    CAP_ROUND_UNIT,
)
from state_schema import create_default_state

DEFAULT_TRADE_RULES: Dict[str, Any] = {
    "trade_deadline": None,
    "salary_cap": 0.0,
    "first_apron": 0.0,
    "second_apron": 0.0,
    "cap_auto_update": True,
    "cap_base_season_year": CAP_BASE_SEASON_YEAR,
    "cap_base_salary_cap": CAP_BASE_SALARY_CAP,
    "cap_base_first_apron": CAP_BASE_FIRST_APRON,
    "cap_base_second_apron": CAP_BASE_SECOND_APRON,
    "cap_annual_growth_rate": CAP_ANNUAL_GROWTH_RATE,
    "cap_round_unit": CAP_ROUND_UNIT,
    "match_small_out_max": 7_500_000,
    "match_mid_out_max": 29_000_000,
    "match_mid_add": 7_500_000,
    "match_buffer": 250_000,
    "first_apron_mult": 1.10,
    "second_apron_mult": 1.00,
    "new_fa_sign_ban_days": 90,
    "aggregation_ban_days": 60,
    "max_pick_years_ahead": 7,
    "stepien_lookahead": 7,
}

_ALLOWED_SCHEDULE_STATUSES = {"scheduled", "final", "in_progress", "canceled"}

_DEFAULT_TRADE_MARKET: Dict[str, Any] = {
    "last_tick_date": None,
    "listings": {},
    "threads": {},
    "cooldowns": {},
    "events": [],
}

_DEFAULT_TRADE_MEMORY: Dict[str, Any] = {
    "relationships": {},
}

_ALLOWED_PHASES = {"regular", "play_in", "playoffs", "preseason"}

_META_PLAYER_KEYS = {"PlayerID", "TeamID", "Name", "Pos", "Position"}

# -------------------------------------------------------------------------
# 1. 전역 상태 및 스케줄/리그 상태 유틸
# -------------------------------------------------------------------------
_STATE: Dict[str, Any] = create_default_state()


def _get_state() -> Dict[str, Any]:
    return _STATE


def reset_state_for_dev() -> None:
    global _STATE
    _STATE = create_default_state()
