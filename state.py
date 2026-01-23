from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from state_modules.state_constants import (
    DEFAULT_TRADE_RULES,
    _ALLOWED_PHASES,
    _ALLOWED_SCHEDULE_STATUSES,
    _DEFAULT_TRADE_MARKET,
    _DEFAULT_TRADE_MEMORY,
    _META_PLAYER_KEYS,
)
from state_modules.state_store import _get_state, reset_state_for_dev as _reset_state_for_dev
from state_schema import validate_game_state

__all__ = [
    "DEFAULT_TRADE_RULES",
    "_ALLOWED_PHASES",
    "_ALLOWED_SCHEDULE_STATUSES",
    "_DEFAULT_TRADE_MARKET",
    "_DEFAULT_TRADE_MEMORY",
    "_META_PLAYER_KEYS",
    "startup_init_state",
    "validate_state",
    "export_workflow_state",
    "export_full_state_snapshot",
    "get_current_date",
    "get_current_date_as_date",
    "set_current_date",
    "get_db_path",
    "set_db_path",
    "set_last_gm_tick_date",
    "get_league_context_snapshot",
    "initialize_master_schedule_if_needed",
    "get_schedule_summary",
    "get_active_season_id",
    "set_active_season_id",
    "ingest_game_result",
    "get_postseason_snapshot",
    "postseason_set_field",
    "postseason_set_play_in",
    "postseason_set_playoffs",
    "postseason_set_champion",
    "postseason_set_my_team_id",
    "postseason_set_dates",
    "postseason_reset",
    "get_cached_stats_snapshot",
    "set_cached_stats_snapshot",
    "get_cached_weekly_news_snapshot",
    "set_cached_weekly_news_snapshot",
    "get_cached_playoff_news_snapshot",
    "set_cached_playoff_news_snapshot",
    "export_trade_context_snapshot",
    "export_trade_assets_snapshot",
    "trade_agreements_get",
    "trade_agreements_set",
    "asset_locks_get",
    "asset_locks_set",
    "negotiations_get",
    "negotiations_set",
    "trade_market_get",
    "trade_market_set",
    "trade_memory_get",
    "trade_memory_set",
    "players_get",
    "players_set",
    "teams_get",
    "teams_set",
    "reset_state_for_dev",
]


def _s() -> dict:
    return _get_state()


def startup_init_state() -> None:
    validate_game_state(_s())
    from state_modules import state_bootstrap
    from state_modules import state_migrations

    state_bootstrap.ensure_db_initialized_and_seeded(_s())
    initialize_master_schedule_if_needed(force=False)
    state_bootstrap.ensure_contracts_bootstrapped_after_schedule_creation_once(_s())
    state_bootstrap.ensure_cap_model_populated_if_needed(_s())
    state_bootstrap.validate_repo_integrity_once_startup(_s())
    state_migrations.ensure_ingest_turn_backfilled_once_startup(_s())
    validate_game_state(_s())


def validate_state() -> None:
    validate_game_state(_s())


def export_workflow_state(
    exclude_keys: tuple[str, ...] = (
        "draft_picks",
        "swap_rights",
        "fixed_assets",
        "transactions",
        "contracts",
        "player_contracts",
        "active_contract_id_by_player",
        "free_agents",
        "gm_profiles",
    ),
) -> dict:
    snapshot = deepcopy(_s())
    for key in exclude_keys:
        snapshot.pop(key, None)
    return snapshot


def export_full_state_snapshot() -> dict:
    return deepcopy(_s())


def get_current_date() -> str | None:
    return _s()["league"]["current_date"]


def get_current_date_as_date():
    league = _s()["league"]
    current_date = league.get("current_date")
    if current_date:
        try:
            return date.fromisoformat(str(current_date))
        except ValueError:
            pass
    season_start = league.get("season_start")
    if season_start:
        try:
            return date.fromisoformat(str(season_start))
        except ValueError:
            pass
    return date.today()


def set_current_date(date_str: str | None) -> None:
    _s()["league"]["current_date"] = date_str
    validate_game_state(_s())


def get_db_path() -> str:
    return str(_s()["league"]["db_path"] or "league.db")


def set_db_path(path: str) -> None:
    _s()["league"]["db_path"] = str(path)
    validate_game_state(_s())


def set_last_gm_tick_date(date_str: str | None) -> None:
    _s()["league"]["last_gm_tick_date"] = date_str
    validate_game_state(_s())


def get_league_context_snapshot() -> dict:
    return {
        "season_year": _s()["league"]["season_year"],
        "trade_rules": deepcopy(_s()["league"]["trade_rules"]),
        "current_date": _s()["league"]["current_date"],
        "season_start": _s()["league"]["season_start"],
    }


def initialize_master_schedule_if_needed(force: bool = False) -> None:
    if force:
        _s()["league"]["master_schedule"]["games"] = []
    from state_modules import state_schedule

    state_schedule.initialize_master_schedule_if_needed(_s())
    validate_game_state(_s())


def get_schedule_summary() -> dict:
    from state_modules import state_schedule

    return state_schedule.get_schedule_summary(_s())


def get_active_season_id() -> str | None:
    return _s()["active_season_id"]


def set_active_season_id(next_season_id: str) -> None:
    old = _s()["active_season_id"]
    if old is not None:
        _s()["season_history"][str(old)] = {
            "regular": deepcopy(
                {
                    "games": _s()["games"],
                    "player_stats": _s()["player_stats"],
                    "team_stats": _s()["team_stats"],
                    "game_results": _s()["game_results"],
                }
            ),
            "phase_results": deepcopy(_s()["phase_results"]),
            "postseason": deepcopy(_s()["postseason"]),
            "archived_at_turn": int(_s()["turn"]),
            "archived_at_date": _s()["league"]["current_date"],
        }
    _s()["games"] = []
    _s()["player_stats"] = {}
    _s()["team_stats"] = {}
    _s()["game_results"] = {}
    _s()["phase_results"] = {
        "preseason": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
        "play_in": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
        "playoffs": {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}},
    }
    _s()["postseason"] = {
        "field": None,
        "play_in": None,
        "playoffs": None,
        "champion": None,
        "my_team_id": None,
        "play_in_start_date": None,
        "play_in_end_date": None,
        "playoffs_start_date": None,
    }
    _s()["active_season_id"] = str(next_season_id)
    _s()["cached_views"] = {
        "scores": {"latest_date": None, "games": []},
        "schedule": {"teams": {}},
        "stats": {"leaders": None},
        "weekly_news": {"last_generated_week_start": None, "items": []},
        "playoff_news": {"series_game_counts": {}, "items": []},
        "_meta": {
            "scores": {"built_from_turn": -1, "season_id": None},
            "schedule": {"built_from_turn_by_team": {}, "season_id": None},
        },
    }
    validate_game_state(_s())


def ingest_game_result(
    game_result: dict,
    game_date: str | None = None,
) -> dict:
    from state_modules import state_results
    from state_modules import state_schedule

    state_results.validate_v2_game_result(game_result)
    _s()["turn"] = int(_s().get("turn", 0) or 0) + 1
    game = game_result["game"]
    phase = str(game["phase"])
    if phase == "regular":
        container = _s()
    elif phase in {"preseason", "play_in", "playoffs"}:
        container = _s()["phase_results"][phase]
    else:
        raise ValueError("invalid phase")

    season_id = str(game["season_id"])
    home_id = str(game["home_team_id"])
    away_id = str(game["away_team_id"])
    final = game_result["final"]
    game_date_str = str(game_date) if game_date else str(game["date"])
    game_id = str(game["game_id"])
    home_score = int(final[home_id])
    away_score = int(final[away_id])
    game_obj = {
        "game_id": game_id,
        "date": game_date_str,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
        "status": "final",
        "is_overtime": int(game.get("overtime_periods", 0) or 0) > 0,
        "phase": phase,
        "season_id": season_id,
        "schema_version": "2.0",
        "ingest_turn": int(_s()["turn"]),
    }

    container["games"].append(game_obj)
    container["game_results"][game_id] = game_result

    teams = game_result["teams"]
    season_player_stats = container["player_stats"]
    season_team_stats = container["team_stats"]
    for tid in (home_id, away_id):
        team_game = teams[tid]
        state_results._accumulate_team_game_result(tid, team_game, season_team_stats)
        rows = team_game.get("players") or []
        if not isinstance(rows, list):
            raise ValueError(f"GameResultV2 invalid: teams.{tid}.players must be list")
        state_results._accumulate_player_rows(rows, season_player_stats)

    state_schedule._mark_master_schedule_game_final(
        _s(),
        game_id=game_id,
        game_date_str=game_date_str,
        home_id=home_id,
        away_id=away_id,
        home_score=home_score,
        away_score=away_score,
    )

    _s()["cached_views"]["_meta"]["scores"]["built_from_turn"] = -1
    _s()["cached_views"]["_meta"]["schedule"]["built_from_turn_by_team"] = {}
    _s()["cached_views"]["stats"]["leaders"] = None
    validate_game_state(_s())
    return game_obj


def validate_v2_game_result(game_result: dict) -> None:
    from state_modules import state_results

    return state_results.validate_v2_game_result(game_result)


def validate_master_schedule_entry(entry: dict, *, path: str = "master_schedule.entry") -> None:
    from state_modules import state_schedule

    return state_schedule.validate_master_schedule_entry(entry, path=path)


def get_postseason_snapshot() -> dict:
    return deepcopy(_s()["postseason"])


def postseason_set_field(field) -> None:
    _s()["postseason"]["field"] = deepcopy(field)
    validate_game_state(_s())


def postseason_set_play_in(state) -> None:
    _s()["postseason"]["play_in"] = deepcopy(state)
    validate_game_state(_s())


def postseason_set_playoffs(state) -> None:
    _s()["postseason"]["playoffs"] = deepcopy(state)
    validate_game_state(_s())


def postseason_set_champion(team_id) -> None:
    _s()["postseason"]["champion"] = team_id
    validate_game_state(_s())


def postseason_set_my_team_id(team_id) -> None:
    _s()["postseason"]["my_team_id"] = team_id
    validate_game_state(_s())


def postseason_set_dates(play_in_start, play_in_end, playoffs_start) -> None:
    _s()["postseason"]["play_in_start_date"] = play_in_start
    _s()["postseason"]["play_in_end_date"] = play_in_end
    _s()["postseason"]["playoffs_start_date"] = playoffs_start
    validate_game_state(_s())


def postseason_reset() -> None:
    _s()["postseason"] = {
        "field": None,
        "play_in": None,
        "playoffs": None,
        "champion": None,
        "my_team_id": None,
        "play_in_start_date": None,
        "play_in_end_date": None,
        "playoffs_start_date": None,
    }
    validate_game_state(_s())


def get_cached_stats_snapshot() -> dict:
    return deepcopy(_s()["cached_views"]["stats"])


def set_cached_stats_snapshot(stats_cache: dict) -> None:
    _s()["cached_views"]["stats"] = deepcopy(stats_cache)
    validate_game_state(_s())


def get_cached_weekly_news_snapshot() -> dict:
    return deepcopy(_s()["cached_views"]["weekly_news"])


def set_cached_weekly_news_snapshot(cache: dict) -> None:
    _s()["cached_views"]["weekly_news"] = deepcopy(cache)
    validate_game_state(_s())


def get_cached_playoff_news_snapshot() -> dict:
    return deepcopy(_s()["cached_views"]["playoff_news"])


def set_cached_playoff_news_snapshot(cache: dict) -> None:
    _s()["cached_views"]["playoff_news"] = deepcopy(cache)
    validate_game_state(_s())


def export_trade_context_snapshot() -> dict:
    return deepcopy(
        {
            "players": _s()["players"],
            "teams": _s()["teams"],
            "asset_locks": _s()["asset_locks"],
            "league": get_league_context_snapshot(),
            "my_team_id": _s()["postseason"]["my_team_id"],
        }
    )


def export_trade_assets_snapshot() -> dict:
    from league_repo import LeagueRepo

    with LeagueRepo(get_db_path()) as repo:
        return deepcopy(repo.get_trade_assets_snapshot() or {})


def ensure_cap_model_populated_if_needed() -> None:
    from state_modules import state_bootstrap

    state_bootstrap.ensure_cap_model_populated_if_needed(_s())
    validate_game_state(_s())


def ensure_player_ids_normalized(*, allow_legacy_numeric: bool = True) -> dict:
    from state_modules import state_bootstrap

    report = state_bootstrap.ensure_player_ids_normalized(_s(), allow_legacy_numeric=allow_legacy_numeric)
    validate_game_state(_s())
    return report


def ensure_trade_state_keys() -> None:
    from state_modules import state_trade

    state_trade.ensure_trade_state_keys(_s())
    validate_game_state(_s())


def trade_agreements_get() -> dict:
    return deepcopy(_s().get("trade_agreements") or {})


def trade_agreements_set(value: dict) -> None:
    _s()["trade_agreements"] = deepcopy(value)
    validate_game_state(_s())


def asset_locks_get() -> dict:
    return deepcopy(_s().get("asset_locks") or {})


def asset_locks_set(value: dict) -> None:
    _s()["asset_locks"] = deepcopy(value)
    validate_game_state(_s())


def negotiations_get() -> dict:
    return deepcopy(_s().get("negotiations") or {})


def negotiations_set(value: dict) -> None:
    _s()["negotiations"] = deepcopy(value)
    validate_game_state(_s())


def trade_market_get() -> dict:
    return deepcopy(_s().get("trade_market") or {})


def trade_market_set(value: dict) -> None:
    _s()["trade_market"] = deepcopy(value)
    validate_game_state(_s())


def trade_memory_get() -> dict:
    return deepcopy(_s().get("trade_memory") or {})


def trade_memory_set(value: dict) -> None:
    _s()["trade_memory"] = deepcopy(value)
    validate_game_state(_s())


def players_get() -> dict:
    return deepcopy(_s().get("players") or {})


def players_set(value: dict) -> None:
    _s()["players"] = deepcopy(value)
    validate_game_state(_s())


def teams_get() -> dict:
    return deepcopy(_s().get("teams") or {})


def teams_set(value: dict) -> None:
    _s()["teams"] = deepcopy(value)
    validate_game_state(_s())


def reset_state_for_dev() -> None:
    _reset_state_for_dev()
