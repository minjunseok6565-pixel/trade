"""Contracts data helpers."""

from __future__ import annotations

import uuid
import math


def new_contract_id() -> str:
    return uuid.uuid4().hex


def _safe_salary(value) -> float:
    try:
        salary = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(salary) or math.isinf(salary):
        return 0.0
    return salary


def make_contract_record(
    contract_id: str,
    player_id: int,
    team_id: str | None,
    signed_date_iso: str,
    start_season_year: int,
    years: int,
    salary_by_year: dict,
    options: list | None = None,
    status: str = "ACTIVE",
) -> dict:
    normalized_salary_by_year = {
        str(key): _safe_salary(value) for key, value in salary_by_year.items()
    }

    return {
        "contract_id": contract_id,
        "player_id": player_id,
        "team_id": team_id,
        "signed_date": signed_date_iso,
        "start_season_year": start_season_year,
        "years": years,
        "salary_by_year": normalized_salary_by_year,
        "options": options or [],
        "status": status,
    }


def get_active_salary_for_season(contract: dict, season_year: int) -> float:
    raw_salary = contract.get("salary_by_year", {}).get(str(season_year), 0.0)
    return _safe_salary(raw_salary)
