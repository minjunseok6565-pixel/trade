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


def _apply_cap_model_for_season(league: Dict[str, Any], season_year: int) -> None:
    """Apply the season-specific cap/apron values to trade rules."""
    trade_rules = league.setdefault("trade_rules", {})
    if trade_rules.get("cap_auto_update") is False:
        return
    try:
        base_season_year = int(
            trade_rules.get("cap_base_season_year", CAP_BASE_SEASON_YEAR)
        )
    except (TypeError, ValueError):
        base_season_year = CAP_BASE_SEASON_YEAR
    try:
        base_salary_cap = float(
            trade_rules.get("cap_base_salary_cap", CAP_BASE_SALARY_CAP)
        )
    except (TypeError, ValueError):
        base_salary_cap = float(CAP_BASE_SALARY_CAP)
    try:
        base_first_apron = float(
            trade_rules.get("cap_base_first_apron", CAP_BASE_FIRST_APRON)
        )
    except (TypeError, ValueError):
        base_first_apron = float(CAP_BASE_FIRST_APRON)
    try:
        base_second_apron = float(
            trade_rules.get("cap_base_second_apron", CAP_BASE_SECOND_APRON)
        )
    except (TypeError, ValueError):
        base_second_apron = float(CAP_BASE_SECOND_APRON)
    try:
        annual_growth_rate = float(
            trade_rules.get("cap_annual_growth_rate", CAP_ANNUAL_GROWTH_RATE)
        )
    except (TypeError, ValueError):
        annual_growth_rate = float(CAP_ANNUAL_GROWTH_RATE)
    try:
        round_unit = int(trade_rules.get("cap_round_unit", CAP_ROUND_UNIT) or 1)
    except (TypeError, ValueError):
        round_unit = CAP_ROUND_UNIT
    if round_unit <= 0:
        round_unit = CAP_ROUND_UNIT or 1

    years_passed = season_year - base_season_year
    multiplier = (1.0 + annual_growth_rate) ** years_passed

    def _round_to_unit(value: float) -> int:
        return int(round(value / round_unit) * round_unit)

    salary_cap = _round_to_unit(base_salary_cap * multiplier)
    first_apron = _round_to_unit(base_first_apron * multiplier)
    second_apron = _round_to_unit(base_second_apron * multiplier)

    if first_apron < salary_cap:
        first_apron = salary_cap
    if second_apron < first_apron:
        second_apron = first_apron

    trade_rules["salary_cap"] = salary_cap
    trade_rules["first_apron"] = first_apron
    trade_rules["second_apron"] = second_apron
