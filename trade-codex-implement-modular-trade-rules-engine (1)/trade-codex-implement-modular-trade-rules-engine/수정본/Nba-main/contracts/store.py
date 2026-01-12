"""Contract state storage helpers."""


def get_league_season_year(game_state: dict) -> int:
    return int(game_state.get("league", {}).get("season_year") or 0)


def get_current_date_iso(game_state: dict) -> str:
    from state import get_current_date_as_date

    return get_current_date_as_date().isoformat()
