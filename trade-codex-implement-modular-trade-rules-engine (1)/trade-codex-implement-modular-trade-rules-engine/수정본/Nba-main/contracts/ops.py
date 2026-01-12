"""Contract operations."""

from __future__ import annotations

import json
from datetime import date

from contracts.models import (
    get_active_salary_for_season,
    make_contract_record,
    new_contract_id,
)
from contracts.store import get_league_season_year
from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id, season_id_from_year


def _resolve_date_iso(game_state: dict, value: "date|str|None") -> str:
    if value is None:
        from state import get_current_date_as_date

        resolved = get_current_date_as_date()
    elif isinstance(value, str):
        resolved = date.fromisoformat(value)
    else:
        resolved = value

    return resolved.isoformat()


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


def _get_team_id(repo: LeagueRepo, player_id: str) -> str:
    row = repo._conn.execute(
        "SELECT team_id FROM roster WHERE player_id=? AND status='active';",
        (player_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"active roster entry not found for player_id={player_id}")
    team_id = row["team_id"]
    if not team_id:
        raise ValueError(f"team_id is missing for player_id={player_id}")
    return str(team_id)


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
    normalized_player_id = _normalize_player_id_str(player_id)
    released_date_iso = _resolve_date_iso(game_state, released_date)

    db_path = _get_db_path(game_state)
    if repo is None:
        with LeagueRepo(db_path) as managed_repo:
            with managed_repo.transaction() as cur:
                now_iso = _utc_now_iso()
                cur.execute(
                    "UPDATE roster SET team_id=?, updated_at=? WHERE player_id=?;",
                    ("FA", now_iso, normalized_player_id),
                )
                cur.execute(
                    """
                    UPDATE contracts
                    SET team_id=?, is_active=0, updated_at=?
                    WHERE player_id=? AND is_active=1;
                    """,
                    ("FA", now_iso, normalized_player_id),
                )
            managed_repo.validate_integrity()
    else:
        if validate is None:
            validate = False
        now_iso = _utc_now_iso()
        cur = cursor or repo._conn
        cur.execute(
            "UPDATE roster SET team_id=?, updated_at=? WHERE player_id=?;",
            ("FA", now_iso, normalized_player_id),
        )
        cur.execute(
            """
            UPDATE contracts
            SET team_id=?, is_active=0, updated_at=?
            WHERE player_id=? AND is_active=1;
            """,
            ("FA", now_iso, normalized_player_id),
        )
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
    normalized_team_id = _normalize_team_id_str(team_id)
    normalized_player_id = _normalize_player_id_str(player_id)
    signed_date_iso = _resolve_date_iso(game_state, signed_date)

    db_path = _get_db_path(game_state)
    if repo is None:
        with LeagueRepo(db_path) as managed_repo:
            current_team_id = _get_team_id(managed_repo, normalized_player_id)
            if current_team_id != "FA":
                raise ValueError(f"Player {normalized_player_id} is not a free agent")
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
        current_team_id = _get_team_id(repo, normalized_player_id)
        if current_team_id != "FA":
            raise ValueError(f"Player {normalized_player_id} is not a free agent")
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

        active_salary = get_active_salary_for_season(contract, start_season_year)
        if cursor is None:
            with repo.transaction() as cur:
                now_iso = _utc_now_iso()
                cur.execute(
                    "UPDATE contracts SET is_active=0, updated_at=? WHERE player_id=? AND is_active=1;",
                    (now_iso, normalized_player_id),
                )
                cur.execute(
                    """
                    INSERT INTO contracts(
                        contract_id,
                        player_id,
                        team_id,
                        start_season_id,
                        end_season_id,
                        salary_by_season_json,
                        contract_type,
                        is_active,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        contract_id,
                        normalized_player_id,
                        normalized_team_id,
                        season_id_from_year(start_season_year),
                        season_id_from_year(start_season_year + years - 1),
                        json.dumps(contract.get("salary_by_year", {})),
                        "STANDARD",
                        1,
                        now_iso,
                        now_iso,
                    ),
                )
                _execute_trade_player(
                    repo, normalized_player_id, normalized_team_id, cursor=cur
                )
                _execute_set_salary(repo, normalized_player_id, active_salary, cursor=cur)
        else:
            now_iso = _utc_now_iso()
            cursor.execute(
                "UPDATE contracts SET is_active=0, updated_at=? WHERE player_id=? AND is_active=1;",
                (now_iso, normalized_player_id),
            )
            cursor.execute(
                """
                INSERT INTO contracts(
                    contract_id,
                    player_id,
                    team_id,
                    start_season_id,
                    end_season_id,
                    salary_by_season_json,
                    contract_type,
                    is_active,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    contract_id,
                    normalized_player_id,
                    normalized_team_id,
                    season_id_from_year(start_season_year),
                    season_id_from_year(start_season_year + years - 1),
                    json.dumps(contract.get("salary_by_year", {})),
                    "STANDARD",
                    1,
                    now_iso,
                    now_iso,
                ),
            )
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

        active_salary = get_active_salary_for_season(contract, start_season_year)
        if cursor is None:
            with repo.transaction() as cur:
                now_iso = _utc_now_iso()
                cur.execute(
                    "UPDATE contracts SET is_active=0, updated_at=? WHERE player_id=? AND is_active=1;",
                    (now_iso, normalized_player_id),
                )
                cur.execute(
                    """
                    INSERT INTO contracts(
                        contract_id,
                        player_id,
                        team_id,
                        start_season_id,
                        end_season_id,
                        salary_by_season_json,
                        contract_type,
                        is_active,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        contract_id,
                        normalized_player_id,
                        normalized_team_id,
                        season_id_from_year(start_season_year),
                        season_id_from_year(start_season_year + years - 1),
                        json.dumps(contract.get("salary_by_year", {})),
                        "STANDARD",
                        1,
                        now_iso,
                        now_iso,
                    ),
                )
                _execute_trade_player(
                    repo, normalized_player_id, normalized_team_id, cursor=cur
                )
                _execute_set_salary(repo, normalized_player_id, active_salary, cursor=cur)
        else:
            now_iso = _utc_now_iso()
            cursor.execute(
                "UPDATE contracts SET is_active=0, updated_at=? WHERE player_id=? AND is_active=1;",
                (now_iso, normalized_player_id),
            )
            cursor.execute(
                """
                INSERT INTO contracts(
                    contract_id,
                    player_id,
                    team_id,
                    start_season_id,
                    end_season_id,
                    salary_by_season_json,
                    contract_type,
                    is_active,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    contract_id,
                    normalized_player_id,
                    normalized_team_id,
                    season_id_from_year(start_season_year),
                    season_id_from_year(start_season_year + years - 1),
                    json.dumps(contract.get("salary_by_year", {})),
                    "STANDARD",
                    1,
                    now_iso,
                    now_iso,
                ),
            )
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
