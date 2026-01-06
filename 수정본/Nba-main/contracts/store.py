"""Contract state storage helpers."""


def ensure_contract_state(game_state: dict) -> None:
    game_state.setdefault("contracts", {})
    game_state.setdefault("player_contracts", {})
    game_state.setdefault("active_contract_id_by_player", {})
    game_state.setdefault("free_agents", [])


def get_league_season_year(game_state: dict) -> int:
    return int(game_state.get("league", {}).get("season_year") or 0)


def get_current_date_iso(game_state: dict) -> str:
    from state import get_current_date_as_date

    return get_current_date_as_date().isoformat()
