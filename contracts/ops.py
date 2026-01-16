"""Contract operations.

Legacy adapter that delegates all writes to DB (SSOT) via LeagueService.
It updates only minimal, best-effort caches inside GAME_STATE.

Do not create or mutate contract/FA ledgers in GAME_STATE from this module.
"""

from __future__ import annotations

from datetime import date

from league_repo import LeagueRepo
from league_service import LeagueService
from schema import normalize_player_id, normalize_team_id


def _resolve_date_iso(game_state: dict, value: date | str | None) -> str:
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
    if not isinstance(league_state, dict):
        raise ValueError("game_state['league'] must be a dict containing 'db_path'")
    db_path = league_state.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required for contract ops")
    return str(db_path)


def _normalize_player_id_str(value) -> str:
    return str(normalize_player_id(value, strict=True))


def _normalize_team_id_str(value) -> str:
    return str(normalize_team_id(value, strict=True, allow_fa=False))


def _update_player_cache_if_present(game_state: dict, player_id: str, updates: dict) -> None:
    """Best-effort cache update: do nothing if players cache isn't ready."""
    try:
        players = game_state.get("players")
        if not isinstance(players, dict):
            return
        p = players.get(player_id)
        if not isinstance(p, dict):
            return
        p.update(updates)
    except Exception:
        return

def release_to_free_agents(
    game_state: dict,
    player_id: str,
    released_date: date | str | None = None,
    *,
    repo: LeagueRepo | None = None,
    validate: bool | None = None,
) -> dict:
    """Release a player to free agency (DB SSOT).

    After migration, contract/FA ledgers in GAME_STATE are no longer reliable.
    This function is kept as a legacy adapter: it updates DB via LeagueService
    and only updates the minimal player cache in GAME_STATE.
    """
    _ensure_team_state(game_state)

    normalized_player_id = _normalize_player_id_str(player_id)
    released_date_iso = _resolve_date_iso(game_state, released_date)

    db_path = _get_db_path(game_state)
    if repo is None:
        with LeagueRepo(db_path) as managed_repo:
            managed_repo.init_db()
            svc = LeagueService(managed_repo)
            svc.release_player_to_free_agency(
                normalized_player_id, released_date=released_date_iso
            )
            managed_repo.validate_integrity()
    else:
        if validate is None:
            validate = False
        svc = LeagueService(repo)
        svc.release_player_to_free_agency(
            normalized_player_id, released_date=released_date_iso
        )
        if validate:
            repo.validate_integrity()

    # Update cache only after DB write succeeds.
    _update_player_cache_if_present(
        game_state,
        normalized_player_id,
        {
            "team_id": "FA",
            "acquired_date": released_date_iso,
            "acquired_via_trade": False,
        },
    )

    return {
        "event": "RELEASE_TO_FREE_AGENTS",
        "player_id": normalized_player_id,
        "released_date": released_date_iso,
    }


def sign_free_agent(
    game_state: dict,
    team_id: str,
    player_id: str,
    signed_date: date | str | None = None,
    years: int = 1,
    salary_by_year: dict | None = None,
    *,
    repo: LeagueRepo | None = None,
    validate: bool | None = None,
) -> dict:
    """Sign a free agent (DB SSOT).

    Legacy adapter that writes through LeagueService and updates only
    the minimal player cache in GAME_STATE (no state ledgers).
    """
    _ensure_team_state(game_state)

    normalized_team_id = _normalize_team_id_str(team_id)
    normalized_player_id = _normalize_player_id_str(player_id)

    signed_date_iso = _resolve_date_iso(game_state, signed_date)

    db_path = _get_db_path(game_state)
    if repo is None:
        with LeagueRepo(db_path) as managed_repo:
            managed_repo.init_db()
            svc = LeagueService(managed_repo)
            evt = svc.sign_free_agent(
                normalized_team_id,
                normalized_player_id,
                signed_date=signed_date_iso,
                years=years,
                salary_by_year=salary_by_year,
            )
            managed_repo.validate_integrity()
    else:
        if validate is None:
            validate = False
        svc = LeagueService(repo)
        evt = svc.sign_free_agent(
            normalized_team_id,
            normalized_player_id,
            signed_date=signed_date_iso,
            years=years,
            salary_by_year=salary_by_year,
        )
        if validate:
            repo.validate_integrity()

    # Keep minimal workflow/UI cache in GAME_STATE.
    _update_player_cache_if_present(
        game_state,
        normalized_player_id,
        {
            "team_id": normalized_team_id,
            "signed_date": signed_date_iso,
            "last_contract_action_date": signed_date_iso,
            "last_contract_action_type": "SIGN_FREE_AGENT",
            "signed_via_free_agency": True,
            "acquired_date": signed_date_iso,
            "acquired_via_trade": False,
        },
    )

    contract_id = str(evt.payload.get("contract_id") or "")

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
    signed_date: date | str | None = None,
    years: int = 1,
    salary_by_year: dict | None = None,
    *,
    repo: LeagueRepo | None = None,
    validate: bool | None = None,
) -> dict:
    """Re-sign / extend a player (DB SSOT).

    Legacy adapter that writes through LeagueService and updates only
    the minimal player cache in GAME_STATE (no state ledgers).
    """
    _ensure_team_state(game_state)

    normalized_team_id = _normalize_team_id_str(team_id)
    normalized_player_id = _normalize_player_id_str(player_id)
    signed_date_iso = _resolve_date_iso(game_state, signed_date)

    db_path = _get_db_path(game_state)
    if repo is None:
        with LeagueRepo(db_path) as managed_repo:
            managed_repo.init_db()
            svc = LeagueService(managed_repo)
            evt = svc.re_sign_or_extend(
                normalized_team_id,
                normalized_player_id,
                signed_date=signed_date_iso,
                years=years,
                salary_by_year=salary_by_year,
            )
            managed_repo.validate_integrity()
    else:
        if validate is None:
            validate = False
        svc = LeagueService(repo)
        evt = svc.re_sign_or_extend(
            normalized_team_id,
            normalized_player_id,
            signed_date=signed_date_iso,
            years=years,
            salary_by_year=salary_by_year,
        )
        if validate:
            repo.validate_integrity()

    # Keep minimal workflow/UI cache in GAME_STATE.
    _update_player_cache_if_present(
        game_state,
        normalized_player_id,
        {
            "team_id": normalized_team_id,
            "signed_date": signed_date_iso,
            "last_contract_action_date": signed_date_iso,
            "last_contract_action_type": "RE_SIGN_OR_EXTEND",
            "signed_via_free_agency": False,
            "acquired_date": signed_date_iso,
            "acquired_via_trade": False,
        },
    )

    contract_id = str(evt.payload.get("contract_id") or "")

    return {
        "event": "RE_SIGN_OR_EXTEND",
        "team_id": normalized_team_id,
        "player_id": normalized_player_id,
        "contract_id": contract_id,
        "signed_date": signed_date_iso,
    }

