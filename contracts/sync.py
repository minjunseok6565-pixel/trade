"""Contract state helpers (DB-first)."""

from __future__ import annotations

from math import isnan

from contracts.models import get_active_salary_for_season
from contracts.store import ensure_contract_state, get_league_season_year


def sync_roster_salaries_for_season(
    game_state: dict, season_year: int, roster_df=None
) -> None:
    """Deprecated: roster DataFrame sync is removed (DB is SSOT)."""
    raise RuntimeError(
        "sync_roster_salaries_for_season is deprecated; "
        "use LeagueRepo for roster updates or export roster from DB."
    )


def sync_roster_teams_from_state(game_state: dict, roster_df=None) -> None:
    """Deprecated: roster DataFrame sync is removed (DB is SSOT)."""
    raise RuntimeError(
        "sync_roster_teams_from_state is deprecated; "
        "use LeagueRepo for roster updates or export roster from DB."
    )


def sync_contract_team_ids_from_players(game_state: dict) -> None:
    from contracts.store import ensure_contract_state

    ensure_contract_state(game_state)

    for player_id_str, contract_id in game_state.get(
        "active_contract_id_by_player", {}
    ).items():
        player_id = str(player_id_str).strip()
        if not player_id:
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
    """Deprecated: roster DataFrame consistency checks removed (DB is SSOT)."""
    ensure_contract_state(game_state)
    if season_year is None:
        season_year = get_league_season_year(game_state)
    _ = (season_year, roster_df, max_errors)
    return None
