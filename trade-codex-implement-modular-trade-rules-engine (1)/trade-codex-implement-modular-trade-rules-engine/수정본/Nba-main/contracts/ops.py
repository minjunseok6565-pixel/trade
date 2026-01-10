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
from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id


def _resolve_date_iso(game_state: dict, value: "date|str|None") -> str:
    if value is None:
        from state import get_current_date_as_date

        resolved = get_current_date_as_date()
    elif isinstance(value, str):
        resolved = date.fromisoformat(value)
    else:
        resolved = value

    return resolved.isoformat()


def _ensure_team_state(game_state: dict) -> None:
    from team_utils import _init_players_and_teams_if_needed

    _init_players_and_teams_if_needed()

def _get_db_path(game_state: dict) -> str:
    league_state = game_state.get("league") or {}
    db_path = league_state.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required for contract ops")
    return db_path


def _normalize_player_id_str(value) -> str:
    return str(normalize_player_id(value, strict=True))


def _normalize_team_id_str(value) -> str:
    return str(normalize_team_id(value, strict=True, allow_fa=False))


def _get_salary_amount(repo: LeagueRepo, player_id: str) -> int:
    row = repo._conn.execute(
        "SELECT salary_amount FROM roster WHERE player_id=? AND status='active';",
        (player_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"active roster entry not found for player_id={player_id}")
    salary_amount = row["salary_amount"]
    if salary_amount is None:
        raise ValueError(f"salary_amount is missing for player_id={player_id}")
    if not isinstance(salary_amount, int):
        raise ValueError(f"salary_amount is not an int for player_id={player_id}")
    return salary_amount


def release_to_free_agents(
    game_state: dict,
    player_id: str,
    released_date: "date|str|None" = None,
) -> dict:
    ensure_contract_state(game_state)
    _ensure_team_state(game_state)

    normalized_player_id = _normalize_player_id_str(player_id)
    released_date_iso = _resolve_date_iso(game_state, released_date)

    player = game_state["players"][normalized_player_id]
    player["team_id"] = ""
    player["acquired_date"] = released_date_iso
    player["acquired_via_trade"] = False

    add_free_agent(game_state, normalized_player_id)

    db_path = _get_db_path(game_state)
    with LeagueRepo(db_path) as repo:
        with repo.transaction():
            repo.release_to_free_agency(normalized_player_id)
        repo.validate_integrity()

    return {
        "event": "RELEASE_TO_FREE_AGENTS",
        "player_id": normalized_player_id,
        "released_date": released_date_iso,
    }


def sign_free_agent(
    game_state: dict,
    team_id: str,
    player_id: str,
    signed_date: "date|str|None" = None,
    years: int = 1,
    salary_by_year: dict | None = None,
) -> dict:
    ensure_contract_state(game_state)
    _ensure_team_state(game_state)

    normalized_team_id = _normalize_team_id_str(team_id)
    normalized_player_id = _normalize_player_id_str(player_id)
    if normalized_player_id not in game_state["free_agents"]:
        raise ValueError(f"Player {normalized_player_id} is not a free agent")

    signed_date_iso = _resolve_date_iso(game_state, signed_date)
    start_season_year = get_league_season_year(game_state)

    db_path = _get_db_path(game_state)
    with LeagueRepo(db_path) as repo:
        if salary_by_year is None:
            base_salary = _get_salary_amount(repo, normalized_player_id)
            salary_by_year = {
                str(year): base_salary
                for year in range(start_season_year, start_season_year + years)
            }

        contract_id = new_contract_id()
        contract = make_contract_record(
            contract_id=contract_id,
            player_id=normalized_player_id,
            team_id=normalized_team_id,
            signed_date_iso=signed_date_iso,
            start_season_year=start_season_year,
            years=years,
            salary_by_year=salary_by_year,
            options=[],
            status="ACTIVE",
        )

        game_state["contracts"][contract_id] = contract
        game_state.setdefault("player_contracts", {}).setdefault(
            str(normalized_player_id), []
        ).append(
            contract_id
        )
        game_state.setdefault("active_contract_id_by_player", {})[
            str(normalized_player_id)
        ] = contract_id

        player = game_state["players"][normalized_player_id]
        player["team_id"] = normalized_team_id
        player["signed_date"] = signed_date_iso
        player["last_contract_action_date"] = signed_date_iso
        player["last_contract_action_type"] = "SIGN_FREE_AGENT"
        player["signed_via_free_agency"] = True
        player["acquired_date"] = signed_date_iso
        player["acquired_via_trade"] = False

        remove_free_agent(game_state, normalized_player_id)

        active_salary = get_active_salary_for_season(contract, start_season_year)
        with repo.transaction():
            repo.trade_player(normalized_player_id, normalized_team_id)
            repo.set_salary(normalized_player_id, active_salary)
        repo.validate_integrity()

    return {
        "event": "SIGN_FREE_AGENT",
        "team_id": normalized_team_id,
        "player_id": normalized_player_id,
        "contract_id": contract_id,
        "signed_date": signed_date_iso,
    }


def re_sign_or_extend(
    game_state: dict,
    team_id: str,
    player_id: str,
    signed_date: "date|str|None" = None,
    years: int = 1,
    salary_by_year: dict | None = None,
) -> dict:
    ensure_contract_state(game_state)
    _ensure_team_state(game_state)

    normalized_team_id = _normalize_team_id_str(team_id)
    normalized_player_id = _normalize_player_id_str(player_id)
    signed_date_iso = _resolve_date_iso(game_state, signed_date)
    start_season_year = get_league_season_year(game_state)

    db_path = _get_db_path(game_state)
    with LeagueRepo(db_path) as repo:
        if salary_by_year is None:
            base_salary = _get_salary_amount(repo, normalized_player_id)
            salary_by_year = {
                str(year): base_salary
                for year in range(start_season_year, start_season_year + years)
            }

        contract_id = new_contract_id()
        contract = make_contract_record(
            contract_id=contract_id,
            player_id=normalized_player_id,
            team_id=normalized_team_id,
            signed_date_iso=signed_date_iso,
            start_season_year=start_season_year,
            years=years,
            salary_by_year=salary_by_year,
            options=[],
            status="ACTIVE",
        )

        game_state["contracts"][contract_id] = contract
        game_state.setdefault("player_contracts", {}).setdefault(
            str(normalized_player_id), []
        ).append(
            contract_id
        )
        game_state.setdefault("active_contract_id_by_player", {})[
            str(normalized_player_id)
        ] = contract_id

        player = game_state["players"][normalized_player_id]
        player["team_id"] = normalized_team_id
        player["signed_date"] = signed_date_iso
        player["last_contract_action_date"] = signed_date_iso
        player["last_contract_action_type"] = "RE_SIGN_OR_EXTEND"
        player["signed_via_free_agency"] = False
        player["acquired_date"] = signed_date_iso
        player["acquired_via_trade"] = False

        active_salary = get_active_salary_for_season(contract, start_season_year)
        with repo.transaction():
            repo.trade_player(normalized_player_id, normalized_team_id)
            repo.set_salary(normalized_player_id, active_salary)
        repo.validate_integrity()

    return {
        "event": "RE_SIGN_OR_EXTEND",
        "team_id": normalized_team_id,
        "player_id": normalized_player_id,
        "contract_id": contract_id,
        "signed_date": signed_date_iso,
    }
