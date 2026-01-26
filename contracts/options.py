"""Contract option utilities."""

from __future__ import annotations

from contracts.options_policy import normalize_option_type


def normalize_option_record(option: dict) -> dict:
    if not isinstance(option, dict):
        raise ValueError("Option record must be a dict")
    if "season_year" not in option or "type" not in option or "status" not in option:
        raise ValueError("Option record missing required keys")

    season_year = int(option["season_year"])
    option_type = normalize_option_type(option["type"])
    status = str(option["status"]).strip().upper()
    if status not in {"PENDING", "EXERCISED", "DECLINED"}:
        raise ValueError(f"Invalid option status: {status}")

    decision_date = option.get("decision_date")
    if decision_date is not None:
        decision_date = str(decision_date)

    return {
        "season_year": season_year,
        "type": option_type,
        "status": status,
        "decision_date": decision_date,
    }


def get_pending_options_for_season(contract: dict, season_year: int) -> list[dict]:
    options = contract.get("options") or []
    return [
        option
        for option in options
        if option.get("season_year") == season_year
        and option.get("status") == "PENDING"
    ]


def apply_option_decision(
    contract: dict,
    option_index: int,
    decision: str,
    decision_date_iso: str,
) -> None:
    decision_value = str(decision).strip().upper()
    if decision_value not in {"EXERCISE", "DECLINE"}:
        raise ValueError(f"Invalid option decision: {decision}")

    option = contract["options"][option_index]
    if decision_value == "EXERCISE":
        option["status"] = "EXERCISED"
    else:
        option["status"] = "DECLINED"
    option["decision_date"] = decision_date_iso

    if decision_value == "DECLINE":
        salary_by_year = contract.get("salary_by_year") or {}
        salary_by_year.pop(str(option["season_year"]), None)


def recompute_contract_years_from_salary(contract: dict) -> None:
    start = int(contract.get("start_season_year") or 0)
    salary_by_year = contract.get("salary_by_year") or {}
    try:
        salary_years = sorted(int(year) for year in salary_by_year.keys())
    except ValueError:
        salary_years = []

    years = 0
    current = start
    while current in salary_years:
        years += 1
        current += 1

    contract["years"] = years
