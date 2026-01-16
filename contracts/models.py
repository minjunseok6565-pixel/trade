"""Contracts data helpers."""

from __future__ import annotations

import math
import uuid

from schema import normalize_player_id, normalize_team_id


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
    player_id: str,
    team_id: str | None,
    signed_date_iso: str,
    start_season_year: int,
    years: int,
    salary_by_year: dict,
    options: list | None = None,
    status: str = "ACTIVE",
) -> dict:
    normalized_player_id = _normalize_player_id(player_id, context="contract.player_id")
    normalized_team_id = (
        _normalize_team_id(team_id, context="contract.team_id") if team_id is not None else None
    )
    normalized_salary_by_year = {
        str(key): _safe_salary(value) for key, value in salary_by_year.items()
    }

    return {
        "contract_id": contract_id,
        "player_id": normalized_player_id,
        "team_id": normalized_team_id,
        "signed_date": signed_date_iso,
        "start_season_year": start_season_year,
        "years": years,
        "salary_by_year": normalized_salary_by_year,
        "options": options or [],
        "status": status,
    }


def _normalize_team_id(value, *, context: str) -> str:
    try:
        return str(normalize_team_id(value, strict=True))
    except ValueError as exc:
        raise ValueError(f"{context}: invalid team_id {value!r}") from exc


def _normalize_player_id(value, *, context: str) -> str:
    try:
        return str(normalize_player_id(value, strict=True))
    except ValueError as exc:
        is_numeric = isinstance(value, str) and value.strip().isdigit()
        is_legacy_int = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        if is_numeric or is_legacy_int:
            try:
                return str(
                    normalize_player_id(
                        str(value),
                        strict=False,
                        allow_legacy_numeric=True,
                    )
                )
            except ValueError as legacy_exc:
                raise ValueError(f"{context}: invalid player_id {value!r}") from legacy_exc
        raise ValueError(f"{context}: invalid player_id {value!r}") from exc


def get_active_salary_for_season(contract: dict, season_year: int) -> float:
    raw_salary = contract.get("salary_by_year", {}).get(str(season_year), 0.0)
    return _safe_salary(raw_salary)
