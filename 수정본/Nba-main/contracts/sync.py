"""Salary synchronization helpers."""

from __future__ import annotations

from math import isnan

from contracts.free_agents import FREE_AGENT_TEAM_ID
from contracts.models import get_active_salary_for_season
from contracts.store import ensure_contract_state, get_league_season_year


def sync_roster_salaries_for_season(
    game_state: dict, season_year: int, roster_df=None
) -> None:
    ensure_contract_state(game_state)

    if roster_df is None:
        from config import ROSTER_DF

        roster_df = ROSTER_DF

    if "SalaryAmount" not in roster_df.columns:
        return

    active_contract_map = game_state.get("active_contract_id_by_player", {})
    contracts = game_state.get("contracts", {})
    for player_id in roster_df.index:
        contract_id = active_contract_map.get(str(player_id))
        contract = contracts.get(contract_id) if contract_id else None
        if contract:
            salary = get_active_salary_for_season(contract, season_year)
            if isinstance(salary, float) and isnan(salary):
                salary = 0.0
            roster_df.at[player_id, "SalaryAmount"] = float(salary)
        else:
            # Zero missing contracts to prevent stale payroll in cached roster data.
            roster_df.at[player_id, "SalaryAmount"] = 0.0


def sync_roster_teams_from_state(game_state: dict, roster_df=None) -> None:
    ensure_contract_state(game_state)

    if roster_df is None:
        from config import ROSTER_DF

        roster_df = ROSTER_DF

    if "Team" not in roster_df.columns:
        return

    for player_id, player_meta in game_state.get("players", {}).items():
        team_id = player_meta.get("team_id")
        if team_id is None or team_id == "":
            normalized_team_id = FREE_AGENT_TEAM_ID
        else:
            normalized_team_id = str(team_id).strip().upper()
        if player_id in roster_df.index:
            roster_df.at[player_id, "Team"] = normalized_team_id


def sync_contract_team_ids_from_players(game_state: dict) -> None:
    from contracts.store import ensure_contract_state

    ensure_contract_state(game_state)

    for player_id_str, contract_id in game_state.get(
        "active_contract_id_by_player", {}
    ).items():
        try:
            player_id = int(player_id_str)
        except (TypeError, ValueError):
            continue
        contract = game_state.get("contracts", {}).get(contract_id)
        if not contract:
            continue
        player_meta = game_state.get("players", {}).get(player_id)
        if not player_meta:
            contract["team_id"] = ""
            continue
        team_id = player_meta.get("team_id")
        if team_id is None or team_id == "":
            normalized_team_id = ""
        else:
            normalized_team_id = str(team_id).strip().upper()
        contract["team_id"] = normalized_team_id


def sync_players_salary_from_active_contract(
    game_state: dict, season_year: int
) -> None:
    from contracts import models
    from contracts.store import ensure_contract_state

    ensure_contract_state(game_state)

    active_contract_map = game_state.get("active_contract_id_by_player", {})
    contracts = game_state.get("contracts", {})
    for player_id, player_meta in game_state.get("players", {}).items():
        contract_id = active_contract_map.get(str(player_id))
        contract = contracts.get(contract_id) if contract_id else None
        if contract:
            expected_salary = models.get_active_salary_for_season(contract, season_year)
            if isinstance(expected_salary, float) and isnan(expected_salary):
                expected_salary = 0.0
            player_meta["salary"] = float(expected_salary)
        else:
            # Zero missing contracts to prevent stale payroll in cached player data.
            player_meta["salary"] = 0.0


def assert_state_vs_roster_consistency(
    game_state: dict,
    season_year: int | None = None,
    roster_df=None,
    max_errors: int = 20,
) -> None:
    ensure_contract_state(game_state)

    if roster_df is None:
        from config import ROSTER_DF

        roster_df = ROSTER_DF

    if season_year is None:
        season_year = get_league_season_year(game_state)

    errors: list[str] = []

    has_team_column = "Team" in roster_df.columns
    for player_id, player_meta in game_state.get("players", {}).items():
        if not has_team_column:
            errors.append("Roster missing Team column for team consistency checks")
            break
        if player_id not in roster_df.index:
            errors.append(f"Player {player_id} missing from roster for team check")
            if len(errors) >= max_errors:
                break
            continue
        expected_team = player_meta.get("team_id")
        if expected_team is None or expected_team == "":
            expected_team = FREE_AGENT_TEAM_ID
        else:
            expected_team = str(expected_team).strip().upper()
        actual_team = roster_df.at[player_id, "Team"]
        if actual_team is None or actual_team == "":
            actual_team = FREE_AGENT_TEAM_ID
        else:
            actual_team = str(actual_team).strip().upper()
        if expected_team != actual_team:
            errors.append(
                f"Team mismatch for player {player_id}: "
                f"state={expected_team} roster={actual_team}"
            )
            if len(errors) >= max_errors:
                break

    has_salary_column = "SalaryAmount" in roster_df.columns
    if not has_salary_column:
        errors.append("Roster missing SalaryAmount column for salary checks")
    else:
        active_contract_map = game_state.get("active_contract_id_by_player", {})
        contracts = game_state.get("contracts", {})
        for player_id in roster_df.index:
            contract_id = active_contract_map.get(str(player_id))
            try:
                player_id = int(player_id)
            except (TypeError, ValueError):
                errors.append(f"Invalid player id in roster index: {player_id}")
                if len(errors) >= max_errors:
                    break
                continue
            contract = contracts.get(contract_id) if contract_id else None
            raw_salary = (
                contract.get("salary_by_year", {}).get(str(season_year))
                if contract
                else 0.0
            )
            expected_salary = (
                get_active_salary_for_season(contract, season_year) if contract else 0.0
            )
            if isinstance(raw_salary, float) and isnan(raw_salary):
                errors.append(
                    "Contract salary is NaN for player "
                    f"{player_id} (contract_id={contract_id})"
                )
                expected_salary = 0.0
            if isinstance(expected_salary, float) and isnan(expected_salary):
                errors.append(
                    "Contract salary is NaN for player "
                    f"{player_id} (contract_id={contract_id})"
                )
                expected_salary = 0.0
            player_meta = game_state.get("players", {}).get(player_id)
            if not player_meta:
                errors.append(f"Player {player_id} missing from state for salary check")
                if len(errors) >= max_errors:
                    break
                continue
            actual_player_salary = player_meta.get("salary", 0.0)
            if actual_player_salary is None or (
                isinstance(actual_player_salary, float) and isnan(actual_player_salary)
            ):
                actual_player_salary = 0.0
            if abs(float(expected_salary) - float(actual_player_salary)) > 0.01:
                errors.append(
                    f"Salary mismatch for player {player_id}: "
                    f"state={expected_salary} player={actual_player_salary}"
                )
                if len(errors) >= max_errors:
                    break
            actual_salary = roster_df.at[player_id, "SalaryAmount"]
            if actual_salary is None or (
                isinstance(actual_salary, float) and isnan(actual_salary)
            ):
                actual_salary = 0.0
            if abs(float(expected_salary) - float(actual_salary)) > 0.01:
                errors.append(
                    f"Salary mismatch for player {player_id}: "
                    f"state={expected_salary} roster={actual_salary}"
                )
                if len(errors) >= max_errors:
                    break

    if errors:
        total = len(errors)
        message_lines = ["Roster consistency check failed:"]
        message_lines.extend(errors[:max_errors])
        if total > max_errors:
            message_lines.append(f"... and {total - max_errors} more")
        raise AssertionError("\n".join(message_lines))
