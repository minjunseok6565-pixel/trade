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


def _utc_now_iso() -> str:
    from datetime import datetime

    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _execute_trade_player(
    repo: LeagueRepo,
    player_id: str,
    to_team_id: str,
    *,
    cursor=None,
) -> None:
    pid = normalize_player_id(player_id, strict=False)
    to_tid = normalize_team_id(to_team_id, strict=True)
    now = _utc_now_iso()
    cur = cursor or repo._conn
    exists = cur.execute(
        "SELECT team_id FROM roster WHERE player_id=? AND status='active';",
        (str(pid),),
    ).fetchone()
    if not exists:
        raise KeyError(f"active roster entry not found for player_id={player_id}")
    cur.execute(
        "UPDATE roster SET team_id=?, updated_at=? WHERE player_id=?;",
        (str(to_tid), now, str(pid)),
    )
    cur.execute(
        "UPDATE contracts SET team_id=?, updated_at=? WHERE player_id=? AND is_active=1;",
        (str(to_tid), now, str(pid)),
    )


def _execute_set_salary(
    repo: LeagueRepo,
    player_id: str,
    salary_amount: int,
    *,
    cursor=None,
) -> None:
    pid = normalize_player_id(player_id, strict=False)
    now = _utc_now_iso()
    cur = cursor or repo._conn
    cur.execute(
        "UPDATE roster SET salary_amount=?, updated_at=? WHERE player_id=?;",
        (int(salary_amount), now, str(pid)),
    )


def release_to_free_agents(
    game_state: dict,
    player_id: str,
    released_date: "date|str|None" = None,
    *,
    repo: LeagueRepo | None = None,
    cursor=None,
    validate: bool | None = None,
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
    if repo is None:
        with LeagueRepo(db_path) as managed_repo:
            with managed_repo.transaction() as cur:
                _execute_trade_player(
                    managed_repo,
                    normalized_player_id,
                    "FA",
                    cursor=cur,
                )
            managed_repo.validate_integrity()
    else:
        if validate is None:
            validate = False
        _execute_trade_player(repo, normalized_player_id, "FA", cursor=cursor)
        if validate:
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
    *,
    repo: LeagueRepo | None = None,
    cursor=None,
    validate: bool | None = None,
) -> dict:
    ensure_contract_state(game_state)
    _ensure_team_state(game_state)

    normalized_team_id = _normalize_team_id_str(team_id)
    normalized_player_id = _normalize_player_id_str(player_id)
    if normalized_player_id not in game_state["free_agents"]:
        raise ValueError(f"Player {normalized_player_id} is not a free agent")

    signed_date_iso = _resolve_date_iso(game_state, signed_date)

    db_path = _get_db_path(game_state)
    if repo is None:
        with LeagueRepo(db_path) as managed_repo:
            return _sign_free_agent_with_repo(
                game_state,
                normalized_team_id,
                normalized_player_id,
                signed_date_iso,
                years,
                salary_by_year,
                repo=managed_repo,
                cursor=None,
                validate=True,
            )
    if validate is None:
        validate = False

    try:
        return _sign_free_agent_with_repo(
            game_state,
            normalized_team_id,
            normalized_player_id,
            signed_date_iso,
            years,
            salary_by_year,
            repo=repo,
            cursor=cursor,
            validate=validate,
        )
    finally:
        pass


def _sign_free_agent_with_repo(
    game_state: dict,
    normalized_team_id: str,
    normalized_player_id: str,
    signed_date_iso: str,
    years: int,
    salary_by_year: dict | None,
    *,
    repo: LeagueRepo,
    cursor=None,
    validate: bool,
) -> dict:
    start_season_year = get_league_season_year(game_state)
    try:
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
        ).append(contract_id)
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
        if cursor is None:
            with repo.transaction() as cur:
                _execute_trade_player(
                    repo, normalized_player_id, normalized_team_id, cursor=cur
                )
                _execute_set_salary(repo, normalized_player_id, active_salary, cursor=cur)
        else:
            _execute_trade_player(
                repo, normalized_player_id, normalized_team_id, cursor=cursor
            )
            _execute_set_salary(repo, normalized_player_id, active_salary, cursor=cursor)
        if validate:
            repo.validate_integrity()
    finally:
        pass

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
    *,
    repo: LeagueRepo | None = None,
    cursor=None,
    validate: bool | None = None,
) -> dict:
    ensure_contract_state(game_state)
    _ensure_team_state(game_state)

    normalized_team_id = _normalize_team_id_str(team_id)
    normalized_player_id = _normalize_player_id_str(player_id)
    signed_date_iso = _resolve_date_iso(game_state, signed_date)

    db_path = _get_db_path(game_state)
    if repo is None:
        with LeagueRepo(db_path) as managed_repo:
            return _re_sign_or_extend_with_repo(
                game_state,
                normalized_team_id,
                normalized_player_id,
                signed_date_iso,
                years,
                salary_by_year,
                repo=managed_repo,
                cursor=None,
                validate=True,
            )
    if validate is None:
        validate = False

    try:
        return _re_sign_or_extend_with_repo(
            game_state,
            normalized_team_id,
            normalized_player_id,
            signed_date_iso,
            years,
            salary_by_year,
            repo=repo,
            cursor=cursor,
            validate=validate,
        )
    finally:
        pass


def _re_sign_or_extend_with_repo(
    game_state: dict,
    normalized_team_id: str,
    normalized_player_id: str,
    signed_date_iso: str,
    years: int,
    salary_by_year: dict | None,
    *,
    repo: LeagueRepo,
    cursor=None,
    validate: bool,
) -> dict:
    start_season_year = get_league_season_year(game_state)
    try:
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
        ).append(contract_id)
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
        if cursor is None:
            with repo.transaction() as cur:
                _execute_trade_player(
                    repo, normalized_player_id, normalized_team_id, cursor=cur
                )
                _execute_set_salary(repo, normalized_player_id, active_salary, cursor=cur)
        else:
            _execute_trade_player(
                repo, normalized_player_id, normalized_team_id, cursor=cursor
            )
            _execute_set_salary(repo, normalized_player_id, active_salary, cursor=cursor)
        if validate:
            repo.validate_integrity()
    finally:
        pass

    return {
        "event": "RE_SIGN_OR_EXTEND",
        "team_id": normalized_team_id,
        "player_id": normalized_player_id,
        "contract_id": contract_id,
        "signed_date": signed_date_iso,
    }
