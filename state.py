from __future__ import annotations

from state_bootstrap import (
    ensure_cap_model_populated_if_needed,
    ensure_contracts_bootstrapped_after_schedule_creation_once,
    ensure_db_initialized_and_seeded,
    ensure_player_ids_normalized,
    validate_repo_integrity_once_startup,
)
from state_cache import _ensure_cached_views_meta, _mark_views_dirty, _reset_cached_views_for_new_season
from state_cap import _apply_cap_model_for_season
from state_core import (
    _archive_and_reset_season_accumulators,
    _ensure_active_season_id,
    _get_phase_container,
    _season_id_from_year,
    ensure_league_block,
    get_current_date,
    get_current_date_as_date,
    set_current_date,
)
from state_migrations import (
    _backfill_ingest_turns_once,
    _ensure_ingest_turn_backfilled,
    ensure_ingest_turn_backfilled_once_startup,
    normalize_player_ids,
)
from state_results import (
    _accumulate_player_rows,
    _accumulate_team_game_result,
    _validate_game_result_v2,
    ingest_game_result,
    validate_v2_game_result,
)
from state_schedule import (
    _build_master_schedule,
    _ensure_master_schedule_indices,
    _ensure_schedule_team,
    _mark_master_schedule_game_final,
    get_schedule_summary,
    initialize_master_schedule_if_needed,
    validate_master_schedule_entry,
)
from state_store import (
    DEFAULT_TRADE_RULES,
    GAME_STATE,
    _ALLOWED_PHASES,
    _ALLOWED_SCHEDULE_STATUSES,
    _DEFAULT_TRADE_MARKET,
    _DEFAULT_TRADE_MEMORY,
    _META_PLAYER_KEYS,
)
from state_trade import _ensure_trade_state, ensure_trade_state_keys
from state_utils import _is_number, _merge_counter_dict_sum, _require_dict, _require_list
from state_views import get_scores_view, get_team_schedule_view

__all__ = [
    "DEFAULT_TRADE_RULES",
    "GAME_STATE",
    "_ALLOWED_PHASES",
    "_ALLOWED_SCHEDULE_STATUSES",
    "_DEFAULT_TRADE_MARKET",
    "_DEFAULT_TRADE_MEMORY",
    "_META_PLAYER_KEYS",
    "_apply_cap_model_for_season",
    "_archive_and_reset_season_accumulators",
    "_backfill_ingest_turns_once",
    "_ensure_active_season_id",
    "_ensure_cached_views_meta",
    "_ensure_ingest_turn_backfilled",
    "_ensure_master_schedule_indices",
    "_ensure_schedule_team",
    "_ensure_trade_state",
    "_get_phase_container",
    "_is_number",
    "_mark_master_schedule_game_final",
    "_mark_views_dirty",
    "_merge_counter_dict_sum",
    "_require_dict",
    "_require_list",
    "_reset_cached_views_for_new_season",
    "_season_id_from_year",
    "_validate_game_result_v2",
    "ensure_cap_model_populated_if_needed",
    "ensure_contracts_bootstrapped_after_schedule_creation_once",
    "ensure_db_initialized_and_seeded",
    "ensure_ingest_turn_backfilled_once_startup",
    "ensure_league_block",
    "ensure_player_ids_normalized",
    "ensure_trade_state_keys",
    "get_current_date",
    "get_current_date_as_date",
    "get_schedule_summary",
    "get_scores_view",
    "get_team_schedule_view",
    "ingest_game_result",
    "initialize_master_schedule_if_needed",
    "normalize_player_ids",
    "set_current_date",
    "validate_master_schedule_entry",
    "validate_repo_integrity_once_startup",
    "validate_v2_game_result",
]
