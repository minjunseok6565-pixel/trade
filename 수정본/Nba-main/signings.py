from __future__ import annotations

from datetime import date

from config import ROSTER_DF
from state import GAME_STATE, get_current_date_as_date
from team_utils import _init_players_and_teams_if_needed


def _normalize_signed_date(signed_date: date | str | None) -> date:
    if signed_date is None:
        return get_current_date_as_date()
    if isinstance(signed_date, str):
        return date.fromisoformat(signed_date)
    if isinstance(signed_date, date):
        return signed_date
    raise TypeError("signed_date must be a date, ISO string, or None")


def _update_roster_team(player_id: int, team_id: str, salary_amount: float | None) -> None:
    if player_id not in ROSTER_DF.index:
        raise KeyError(f"player_id {player_id} not found in roster")
    ROSTER_DF.at[player_id, "Team"] = team_id
    if salary_amount is not None and "SalaryAmount" in ROSTER_DF.columns:
        ROSTER_DF.at[player_id, "SalaryAmount"] = float(salary_amount)


def _update_player_salary(player_state: dict, salary_amount: float | None) -> None:
    if salary_amount is not None and "salary" in player_state:
        player_state["salary"] = float(salary_amount)


def sign_free_agent(
    game_state: dict,
    team_id: str,
    player_id: int,
    signed_date: date | str | None = None,
    salary_amount: float | None = None,
) -> dict:
    _init_players_and_teams_if_needed()
    team = str(team_id).upper()
    effective_date = _normalize_signed_date(signed_date)
    iso_date = effective_date.isoformat()

    player_state = GAME_STATE["players"].get(player_id)
    if player_state is None:
        raise ValueError(f"player_id {player_id} not found in GAME_STATE")

    player_state["team_id"] = team
    player_state["signed_date"] = iso_date
    player_state["signed_via_free_agency"] = True
    player_state["acquired_date"] = iso_date
    player_state["acquired_via_trade"] = False
    _update_player_salary(player_state, salary_amount)

    _update_roster_team(player_id, team, salary_amount)

    return {
        "event": "sign_free_agent",
        "team_id": team,
        "player_id": player_id,
        "signed_date": iso_date,
        "salary_amount": salary_amount,
    }


def re_sign_or_extend(
    game_state: dict,
    team_id: str,
    player_id: int,
    signed_date: date | str | None = None,
    salary_amount: float | None = None,
) -> dict:
    _init_players_and_teams_if_needed()
    team = str(team_id).upper()
    effective_date = _normalize_signed_date(signed_date)
    iso_date = effective_date.isoformat()

    player_state = GAME_STATE["players"].get(player_id)
    if player_state is None:
        raise ValueError(f"player_id {player_id} not found in GAME_STATE")

    player_state["team_id"] = team
    player_state["signed_date"] = iso_date
    player_state["signed_via_free_agency"] = False
    player_state["acquired_date"] = iso_date
    player_state["acquired_via_trade"] = False
    _update_player_salary(player_state, salary_amount)

    _update_roster_team(player_id, team, salary_amount)

    return {
        "event": "re_sign_or_extend",
        "team_id": team,
        "player_id": player_id,
        "signed_date": iso_date,
        "salary_amount": salary_amount,
    }
