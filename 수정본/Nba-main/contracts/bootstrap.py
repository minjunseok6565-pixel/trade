"""Bootstrap contracts from roster Excel-derived data."""

from __future__ import annotations

import math
import re
from datetime import date


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        if value != value:
            return True
    except Exception:
        pass
    try:
        return math.isnan(value)
    except Exception:
        return False


def _parse_int_like(value) -> int | None:
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned == "":
            return None
        try:
            return int(float(cleaned))
        except ValueError:
            return None
    return None


def _parse_salary(value) -> float | None:
    if _is_blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        if cleaned == "":
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _parse_bool_like(value) -> bool | None:
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def _parse_iso_date(value) -> str | None:
    if _is_blank(value):
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    return None


def bootstrap_contracts_from_roster_excel(
    game_state: dict,
    roster_df=None,
    overwrite: bool = False,
) -> dict:
    from contracts import models
    from contracts.options_policy import normalize_option_type
    from contracts.store import (
        ensure_contract_state,
        get_current_date_iso,
        get_league_season_year,
    )
    import team_utils

    ensure_contract_state(game_state)
    team_utils._init_players_and_teams_if_needed()

    if roster_df is None:
        from config import ROSTER_DF

        roster_df = ROSTER_DF

    if not overwrite and game_state.get("contracts"):
        return {
            "skipped": True,
            "reason": "contracts already exist",
            "initial_free_agents": [],
            "skipped_contracts_for_fa": 0,
            "created": 0,
        }

    league_season_year = get_league_season_year(game_state)

    salary_years = []
    for column in roster_df.columns:
        match = re.match(r"^Salary_(\d{4})$", str(column))
        if match:
            salary_years.append(int(match.group(1)))
    salary_years.sort()
    used_salary_columns = [f"Salary_{year}" for year in salary_years]

    players = game_state.get("players", {})
    missing_players = []
    created = 0
    skipped_contracts_for_fa = 0
    initial_free_agents: list[int] = []
    has_team_column = "Team" in roster_df.columns

    for player_id in roster_df.index:
        player_key = str(player_id)
        if player_key not in players:
            missing_players.append(player_id)
            continue

        is_free_agent = False
        if has_team_column:
            team_value = roster_df.at[player_id, "Team"]
            if _is_blank(team_value):
                is_free_agent = True
            elif isinstance(team_value, str) and team_value.strip().upper() == "FA":
                is_free_agent = True
        if is_free_agent:
            players[player_key]["team_id"] = ""
            initial_free_agents.append(player_id)
            skipped_contracts_for_fa += 1
            continue

        start_season_year = None
        if "ContractStartSeasonYear" in roster_df.columns:
            start_season_year = _parse_int_like(
                roster_df.at[player_id, "ContractStartSeasonYear"]
            )
        if not start_season_year or start_season_year <= 0:
            start_season_year = league_season_year

        salary_by_year = {}
        for year in salary_years:
            column = f"Salary_{year}"
            cell = roster_df.at[player_id, column]
            parsed_salary = _parse_salary(cell)
            if parsed_salary is None:
                continue
            salary_by_year[str(year)] = parsed_salary

        years = None
        if "ContractYears" in roster_df.columns:
            years = _parse_int_like(roster_df.at[player_id, "ContractYears"])
        if not years or years <= 0:
            years = 0
            for offset in range(len(salary_years)):
                season_year = start_season_year + offset
                if str(season_year) in salary_by_year:
                    years += 1
                else:
                    break
            if years <= 0:
                years = 1

        if not salary_by_year:
            fallback_salary = 0.0
            if "SalaryAmount" in roster_df.columns:
                fallback_salary = _parse_salary(
                    roster_df.at[player_id, "SalaryAmount"]
                ) or 0.0
            salary_by_year[str(start_season_year)] = float(fallback_salary)

        options = []
        for idx in (1, 2):
            option_type_col = f"Option{idx}Type"
            option_year_col = f"Option{idx}SeasonYear"
            if option_type_col not in roster_df.columns:
                continue
            if option_year_col not in roster_df.columns:
                continue
            option_type_raw = roster_df.at[player_id, option_type_col]
            option_year_raw = roster_df.at[player_id, option_year_col]
            option_year = _parse_int_like(option_year_raw)
            if _is_blank(option_type_raw) or not option_year:
                continue
            try:
                option_type = normalize_option_type(option_type_raw)
            except ValueError:
                continue
            options.append(
                {
                    "season_year": int(option_year),
                    "type": option_type,
                    "status": "PENDING",
                    "decision_date": None,
                }
            )

        team_id = players[player_key].get("team_id")
        if isinstance(team_id, str):
            team_id = team_id.upper()
        else:
            team_id = ""

        signed_date_iso = None
        if "SignedDate" in roster_df.columns:
            signed_date_iso = _parse_iso_date(roster_df.at[player_id, "SignedDate"])
        if not signed_date_iso:
            signed_date_iso = get_current_date_iso(game_state)

        contract_id = models.new_contract_id()
        contract = models.make_contract_record(
            contract_id=contract_id,
            player_id=player_id,
            team_id=team_id,
            signed_date_iso=signed_date_iso,
            start_season_year=start_season_year,
            years=years,
            salary_by_year=salary_by_year,
            options=options,
            status="ACTIVE",
        )

        game_state["contracts"][contract_id] = contract
        game_state.setdefault("player_contracts", {}).setdefault(player_key, []).append(
            contract_id
        )
        game_state.setdefault("active_contract_id_by_player", {})[player_key] = (
            contract_id
        )

        if "SignedViaFreeAgency" in roster_df.columns:
            signed_via_free_agency = _parse_bool_like(
                roster_df.at[player_id, "SignedViaFreeAgency"]
            )
            if signed_via_free_agency is not None:
                players[player_key]["signed_via_free_agency"] = signed_via_free_agency
        if "SignedDate" in roster_df.columns:
            players[player_key]["signed_date"] = signed_date_iso

        created += 1

    if len(initial_free_agents) != len(set(initial_free_agents)):
        deduped: list[int] = []
        seen: set[int] = set()
        for player_id in initial_free_agents:
            if player_id in seen:
                continue
            deduped.append(player_id)
            seen.add(player_id)
        initial_free_agents = deduped

    game_state["free_agents"] = list(initial_free_agents)

    return {
        "skipped": False,
        "created": created,
        "missing_players": missing_players,
        "used_salary_columns": used_salary_columns,
        "initial_free_agents": list(initial_free_agents),
        "skipped_contracts_for_fa": skipped_contracts_for_fa,
    }
