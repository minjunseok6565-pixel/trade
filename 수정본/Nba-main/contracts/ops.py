"""Contract operations."""

from __future__ import annotations

from datetime import date

from contracts.free_agents import add_free_agent, remove_free_agent
from contracts.models import (
    get_active_salary_for_season,
    make_contract_record,
    new_contract_id,
)
from contracts.store import ensure_contract_state, get_league_season_year


def _resolve_date_iso(game_state: dict, value: "date|str|None") -> str:
    if value is None:
        from state import get_current_date_as_date

        resolved = get_current_date_as_date(game_state)
    elif isinstance(value, str):
        resolved = date.fromisoformat(value)
    else:
        resolved = value

    return resolved.isoformat()


def _ensure_team_state(game_state: dict) -> None:
    from team_utils import _init_players_and_teams_if_needed

    _init_players_and_teams_if_needed(game_state)


def release_to_free_agents(
    game_state: dict,
    player_id: int,
    released_date: "date|str|None" = None,
) -> dict:
    ensure_contract_state(game_state)
    _ensure_team_state(game_state)

    released_date_iso = _resolve_date_iso(game_state, released_date)

    player = game_state["players"][player_id]
    player["team_id"] = ""
    player["acquired_date"] = released_date_iso
    player["acquired_via_trade"] = False

    add_free_agent(game_state, player_id)

    from config import ROSTER_DF

    if player_id not in ROSTER_DF.index:
        raise KeyError(f"Player ID {player_id} not found in roster dataframe")
    ROSTER_DF.at[player_id, "Team"] = "FA"

    from contracts.store import get_league_season_year
    from contracts.sync import (
        sync_contract_team_ids_from_players,
        sync_players_salary_from_active_contract,
        sync_roster_salaries_for_season,
        sync_roster_teams_from_state,
    )

    season_year = get_league_season_year(game_state)
    sync_contract_team_ids_from_players(game_state)
    sync_players_salary_from_active_contract(game_state, season_year)
    sync_roster_teams_from_state(game_state)
    sync_roster_salaries_for_season(game_state, season_year)

    return {
        "event": "RELEASE_TO_FREE_AGENTS",
        "player_id": player_id,
        "released_date": released_date_iso,
    }


def sign_free_agent(
    game_state: dict,
    team_id: str,
    player_id: int,
    signed_date: "date|str|None" = None,
    years: int = 1,
    salary_by_year: dict | None = None,
) -> dict:
    ensure_contract_state(game_state)
    _ensure_team_state(game_state)

    team_id = team_id.upper()
    if player_id not in game_state["free_agents"]:
        raise ValueError(f"Player {player_id} is not a free agent")

    signed_date_iso = _resolve_date_iso(game_state, signed_date)
    start_season_year = get_league_season_year(game_state)

    from config import ROSTER_DF

    if salary_by_year is None:
        if player_id not in ROSTER_DF.index:
            raise KeyError(f"Player ID {player_id} not found in roster dataframe")
        base_salary = ROSTER_DF.at[player_id, "SalaryAmount"]
        salary_by_year = {
            str(year): base_salary
            for year in range(start_season_year, start_season_year + years)
        }

    contract_id = new_contract_id()
    contract = make_contract_record(
        contract_id=contract_id,
        player_id=player_id,
        team_id=team_id,
        signed_date_iso=signed_date_iso,
        start_season_year=start_season_year,
        years=years,
        salary_by_year=salary_by_year,
        options=[],
        status="ACTIVE",
    )

    game_state["contracts"][contract_id] = contract
    game_state.setdefault("player_contracts", {}).setdefault(str(player_id), []).append(
        contract_id
    )
    game_state.setdefault("active_contract_id_by_player", {})[str(player_id)] = contract_id

    player = game_state["players"][player_id]
    player["team_id"] = team_id
    player["signed_date"] = signed_date_iso
    player["signed_via_free_agency"] = True
    player["acquired_date"] = signed_date_iso
    player["acquired_via_trade"] = False

    remove_free_agent(game_state, player_id)

    if player_id not in ROSTER_DF.index:
        raise KeyError(f"Player ID {player_id} not found in roster dataframe")
    ROSTER_DF.at[player_id, "Team"] = team_id
    ROSTER_DF.at[player_id, "SalaryAmount"] = get_active_salary_for_season(
        contract, start_season_year
    )

    from contracts.store import get_league_season_year
    from contracts.sync import (
        sync_contract_team_ids_from_players,
        sync_players_salary_from_active_contract,
        sync_roster_salaries_for_season,
        sync_roster_teams_from_state,
    )

    season_year = get_league_season_year(game_state)
    sync_contract_team_ids_from_players(game_state)
    sync_players_salary_from_active_contract(game_state, season_year)
    sync_roster_teams_from_state(game_state)
    sync_roster_salaries_for_season(game_state, season_year)

    return {
        "event": "SIGN_FREE_AGENT",
        "team_id": team_id,
        "player_id": player_id,
        "contract_id": contract_id,
        "signed_date": signed_date_iso,
    }


def re_sign_or_extend(
    game_state: dict,
    team_id: str,
    player_id: int,
    signed_date: "date|str|None" = None,
    years: int = 1,
    salary_by_year: dict | None = None,
) -> dict:
    ensure_contract_state(game_state)
    _ensure_team_state(game_state)

    team_id = team_id.upper()
    signed_date_iso = _resolve_date_iso(game_state, signed_date)
    start_season_year = get_league_season_year(game_state)

    from config import ROSTER_DF

    if salary_by_year is None:
        if player_id not in ROSTER_DF.index:
            raise KeyError(f"Player ID {player_id} not found in roster dataframe")
        base_salary = ROSTER_DF.at[player_id, "SalaryAmount"]
        salary_by_year = {
            str(year): base_salary
            for year in range(start_season_year, start_season_year + years)
        }

    contract_id = new_contract_id()
    contract = make_contract_record(
        contract_id=contract_id,
        player_id=player_id,
        team_id=team_id,
        signed_date_iso=signed_date_iso,
        start_season_year=start_season_year,
        years=years,
        salary_by_year=salary_by_year,
        options=[],
        status="ACTIVE",
    )

    game_state["contracts"][contract_id] = contract
    game_state.setdefault("player_contracts", {}).setdefault(str(player_id), []).append(
        contract_id
    )
    game_state.setdefault("active_contract_id_by_player", {})[str(player_id)] = contract_id

    player = game_state["players"][player_id]
    player["team_id"] = team_id
    player["signed_date"] = signed_date_iso
    player["signed_via_free_agency"] = False
    player["acquired_date"] = signed_date_iso
    player["acquired_via_trade"] = False

    if player_id not in ROSTER_DF.index:
        raise KeyError(f"Player ID {player_id} not found in roster dataframe")
    ROSTER_DF.at[player_id, "Team"] = team_id
    ROSTER_DF.at[player_id, "SalaryAmount"] = get_active_salary_for_season(
        contract, start_season_year
    )

    from contracts.store import get_league_season_year
    from contracts.sync import (
        sync_contract_team_ids_from_players,
        sync_players_salary_from_active_contract,
        sync_roster_salaries_for_season,
        sync_roster_teams_from_state,
    )

    season_year = get_league_season_year(game_state)
    sync_contract_team_ids_from_players(game_state)
    sync_players_salary_from_active_contract(game_state, season_year)
    sync_roster_teams_from_state(game_state)
    sync_roster_salaries_for_season(game_state, season_year)

    return {
        "event": "RE_SIGN_OR_EXTEND",
        "team_id": team_id,
        "player_id": player_id,
        "contract_id": contract_id,
        "signed_date": signed_date_iso,
    }
