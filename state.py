from __future__ import annotations

from copy import deepcopy
from typing import Any

from state_modules.state_constants import (
    DEFAULT_TRADE_RULES,
    _ALLOWED_PHASES,
    _ALLOWED_SCHEDULE_STATUSES,
    _DEFAULT_TRADE_MARKET,
    _DEFAULT_TRADE_MEMORY,
    _META_PLAYER_KEYS,
)
from state_modules.state_store import get_state, reset_game_state
from state_schema import validate_game_state

from state_modules import state_bootstrap
from state_modules import state_core
from state_modules import state_results
from state_modules import state_schedule
from state_modules import state_trade
from state_modules import state_views


__all__ = [
    "DEFAULT_TRADE_RULES",
    "_ALLOWED_PHASES",
    "_ALLOWED_SCHEDULE_STATUSES",
    "_DEFAULT_TRADE_MARKET",
    "_DEFAULT_TRADE_MEMORY",
    "_META_PLAYER_KEYS",
    "ensure_league_block",
    "get_current_date",
    "get_current_date_as_date",
    "set_current_date",
    "initialize_master_schedule_if_needed",
    "get_schedule_summary",
    "get_scores_view",
    "get_team_schedule_view",
    "ingest_game_result",
    "validate_v2_game_result",
    "validate_master_schedule_entry",
    "ensure_db_initialized_and_seeded",
    "ensure_cap_model_populated_if_needed",
    "ensure_trade_state_keys",
    "ensure_player_ids_normalized",
    "validate_repo_integrity_once_startup",
    "ensure_ingest_turn_backfilled_once_startup",
    "export_workflow_state",
    "postseason_get_state",
    "postseason_set",
    "cached_view_get",
    "cached_view_set",
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
    "league_get",
    "set_league_value",
    "reset_game_state",
]


def _s() -> dict:
    return get_state()


def ensure_league_block() -> dict:
    league = state_core.ensure_league_block(_s())
    validate_game_state(_s())
    return deepcopy(league)


def get_current_date() -> str | None:
    return state_core.get_current_date(_s())


def get_current_date_as_date():
    return state_core.get_current_date_as_date(_s())


def set_current_date(date_str: str | None) -> None:
    state_core.set_current_date(_s(), date_str)
    validate_game_state(_s())


def initialize_master_schedule_if_needed() -> None:
    state_schedule.initialize_master_schedule_if_needed(_s())
    validate_game_state(_s())


def get_schedule_summary() -> dict:
    return state_schedule.get_schedule_summary(_s())


def get_scores_view(season_id: str, limit: int = 20) -> dict:
    return state_views.get_scores_view(_s(), season_id, limit=limit)


def get_team_schedule_view(team_id: str, season_id: str, today: str | None = None) -> dict:
    return state_views.get_team_schedule_view(_s(), team_id, season_id, today=today)


def ingest_game_result(
    game_result: dict,
    game_date: str | None = None,
    store_raw: bool = True,
) -> dict:
    result = state_results.ingest_game_result(
        _s(),
        game_result=game_result,
        game_date=game_date,
        store_raw_result=store_raw,
    )
    validate_game_state(_s())
    return result


def validate_v2_game_result(game_result: dict) -> None:
    return state_results.validate_v2_game_result(game_result)


def validate_master_schedule_entry(entry: dict, *, path: str = "master_schedule.entry") -> None:
    return state_schedule.validate_master_schedule_entry(entry, path=path)


def ensure_db_initialized_and_seeded() -> None:
    state_bootstrap.ensure_db_initialized_and_seeded(_s())
    validate_game_state(_s())


def ensure_cap_model_populated_if_needed() -> None:
    state_bootstrap.ensure_cap_model_populated_if_needed(_s())
    validate_game_state(_s())


def ensure_trade_state_keys() -> None:
    state_trade.ensure_trade_state_keys(_s())
    validate_game_state(_s())


def ensure_player_ids_normalized(*, allow_legacy_numeric: bool = True) -> dict:
    report = state_bootstrap.ensure_player_ids_normalized(_s(), allow_legacy_numeric=allow_legacy_numeric)
    validate_game_state(_s())
    return report


def validate_repo_integrity_once_startup() -> None:
    state_bootstrap.validate_repo_integrity_once_startup(_s())
    validate_game_state(_s())


def ensure_ingest_turn_backfilled_once_startup() -> None:
    return None


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


def postseason_get_state() -> dict:
    return deepcopy(_s().get("postseason") or {})


def postseason_set(key: str, value: Any) -> None:
    postseason = _s().setdefault("postseason", {})
    postseason[key] = deepcopy(value)
    validate_game_state(_s())


def cached_view_get(key: str) -> Any:
    cached_views = _s().get("cached_views") or {}
    return deepcopy(cached_views.get(key))


def cached_view_set(key: str, value: Any) -> None:
    cached_views = _s().setdefault("cached_views", {})
    cached_views[key] = deepcopy(value)
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


def league_get() -> dict:
    return deepcopy(_s().get("league") or {})


def set_league_value(key: str, value: Any) -> None:
    league = _s().setdefault("league", {})
    league[key] = deepcopy(value)
    validate_game_state(_s())
