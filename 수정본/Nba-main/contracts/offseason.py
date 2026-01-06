"""Offseason contract handling."""

from __future__ import annotations


def process_offseason(
    game_state: dict,
    from_season_year: int,
    to_season_year: int,
    decision_policy=None,
) -> dict:
    from contracts.options import (
        apply_option_decision,
        get_pending_options_for_season,
        normalize_option_record,
        recompute_contract_years_from_salary,
    )
    from contracts.options_policy import default_option_decision_policy
    from contracts.store import ensure_contract_state, get_current_date_iso

    ensure_contract_state(game_state)

    contracts = game_state.get("contracts", {})
    active_map = game_state.get("active_contract_id_by_player", {})
    expired = 0
    released = 0
    decision_date_iso = get_current_date_iso(game_state)
    if decision_policy is None:
        decision_policy = default_option_decision_policy

    for player_id_str, contract_id in list(active_map.items()):
        contract = contracts.get(contract_id)
        if not contract:
            continue
        contract_options = contract.get("options") or []
        try:
            contract["options"] = [
                normalize_option_record(option) for option in contract_options
            ]
        except ValueError:
            contract["options"] = []
        try:
            player_id = int(player_id_str)
        except (TypeError, ValueError):
            player_id = None
        pending = get_pending_options_for_season(contract, to_season_year)
        if pending:
            for option_index, option in enumerate(contract["options"]):
                if option.get("season_year") != to_season_year:
                    continue
                if option.get("status") != "PENDING":
                    continue
                decision = decision_policy(option, player_id, contract, game_state)
                apply_option_decision(
                    contract,
                    option_index,
                    decision,
                    decision_date_iso,
                )
            recompute_contract_years_from_salary(contract)
        try:
            start = int(contract.get("start_season_year") or 0)
        except (TypeError, ValueError):
            start = 0
        try:
            years = int(contract.get("years") or 0)
        except (TypeError, ValueError):
            years = 0
        end_exclusive = start + years
        if to_season_year >= end_exclusive:
            contract["status"] = "EXPIRED"
            active_map.pop(player_id_str, None)
            try:
                player_id = int(player_id_str)
            except (TypeError, ValueError):
                continue
            from contracts.ops import release_to_free_agents

            release_to_free_agents(game_state, player_id, released_date=None)
            expired += 1
            released += 1

    from contracts.sync import (
        sync_contract_team_ids_from_players,
        sync_players_salary_from_active_contract,
        sync_roster_salaries_for_season,
        sync_roster_teams_from_state,
    )

    sync_roster_salaries_for_season(game_state, to_season_year)
    sync_players_salary_from_active_contract(game_state, to_season_year)
    sync_contract_team_ids_from_players(game_state)
    sync_roster_teams_from_state(game_state)

    return {"expired": expired, "released": released}
