from __future__ import annotations

from typing import Any, Dict

GAME_STATE_SCHEMA_VERSION = "3.0"
ALLOWED_PHASES = {"regular", "play_in", "playoffs", "preseason"}
NON_REGULAR_PHASES = {"play_in", "playoffs", "preseason"}

_PHASE_RESULTS_KEYS = {"games", "player_stats", "team_stats", "game_results"}
_POSTSEASON_KEYS = {"field", "play_in", "playoffs", "champion", "my_team_id"}


def create_default_postseason_state() -> dict:
    return {
        "field": None,
        "play_in": None,
        "playoffs": None,
        "champion": None,
        "my_team_id": None,
    }


def create_default_phase_results() -> dict:
    return {"games": [], "player_stats": {}, "team_stats": {}, "game_results": {}}


def create_default_cached_views() -> dict:
    return {
        "_meta": {
            "scores": {"built_from_turn": -1, "season_id": None},
            "schedule": {"built_from_turn_by_team": {}},
        },
        "scores": None,
        "schedule": None,
        "stats": {"leaders": None},
        "weekly_news": None,
        "playoff_news": None,
    }


def create_default_league_state() -> dict:
    return {
        "season_year": None,
        "draft_year": None,
        "season_start": None,
        "current_date": None,
        "trade_rules": {},
        "master_schedule": {
            "by_id": {},
            "games": [],
            "version": 1,
            "by_team": {},
            "by_date": {},
        },
        "db_path": None,
        "last_gm_tick_date": None,
    }


def create_default_state() -> dict:
    return {
        "schema_version": GAME_STATE_SCHEMA_VERSION,
        "turn": 0,
        "active_season_id": None,
        "season_history": {},
        "games": [],
        "player_stats": {},
        "team_stats": {},
        "game_results": {},
        "phase_results": {},
        "cached_views": create_default_cached_views(),
        "league": create_default_league_state(),
        "teams": {},
        "players": {},
        "trade_agreements": {},
        "negotiations": {},
        "asset_locks": {},
        "trade_market": {},
        "trade_memory": {},
        "draft_picks": {},
        "swap_rights": {},
        "fixed_assets": {},
        "transactions": [],
        "contracts": {},
        "player_contracts": {},
        "active_contract_id_by_player": {},
        "free_agents": [],
        "gm_profiles": {},
        "postseason": create_default_postseason_state(),
    }


ALLOWED_TOP_LEVEL_KEYS = set(create_default_state().keys())


def _require_dict(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a dict")


def _require_list(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")


def _validate_phase_results_block(block: Any, label: str) -> None:
    _require_dict(block, label)
    if set(block.keys()) != _PHASE_RESULTS_KEYS:
        raise ValueError(f"{label} must have keys {_PHASE_RESULTS_KEYS}")
    _require_list(block.get("games"), f"{label}.games")
    _require_dict(block.get("player_stats"), f"{label}.player_stats")
    _require_dict(block.get("team_stats"), f"{label}.team_stats")
    _require_dict(block.get("game_results"), f"{label}.game_results")


def _validate_postseason_block(block: Any, label: str) -> None:
    _require_dict(block, label)
    if set(block.keys()) != _POSTSEASON_KEYS:
        raise ValueError(f"{label} must have keys {_POSTSEASON_KEYS}")


def validate_game_state(state: dict) -> None:
    if not isinstance(state, dict):
        raise ValueError("state must be a dict")

    unknown_keys = set(state.keys()) - ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        unknown = sorted(unknown_keys)[0]
        raise ValueError(f"Unknown top-level key: {unknown}")

    if state.get("schema_version") != GAME_STATE_SCHEMA_VERSION:
        raise ValueError("schema_version mismatch")

    turn = state.get("turn")
    if not isinstance(turn, int) or turn < 0:
        raise ValueError("turn must be a non-negative int")

    active_season_id = state.get("active_season_id")
    if active_season_id is not None and not isinstance(active_season_id, str):
        raise ValueError("active_season_id must be None or str")

    _require_dict(state.get("season_history"), "season_history")
    _require_list(state.get("games"), "games")
    _require_dict(state.get("player_stats"), "player_stats")
    _require_dict(state.get("team_stats"), "team_stats")
    _require_dict(state.get("game_results"), "game_results")
    _require_dict(state.get("phase_results"), "phase_results")
    _require_dict(state.get("cached_views"), "cached_views")
    _require_dict(state.get("league"), "league")
    _require_dict(state.get("teams"), "teams")
    _require_dict(state.get("players"), "players")
    _require_dict(state.get("trade_agreements"), "trade_agreements")
    _require_dict(state.get("negotiations"), "negotiations")
    _require_dict(state.get("asset_locks"), "asset_locks")
    _require_dict(state.get("trade_market"), "trade_market")
    _require_dict(state.get("trade_memory"), "trade_memory")
    _require_dict(state.get("draft_picks"), "draft_picks")
    _require_dict(state.get("swap_rights"), "swap_rights")
    _require_dict(state.get("fixed_assets"), "fixed_assets")
    _require_list(state.get("transactions"), "transactions")
    _require_dict(state.get("contracts"), "contracts")
    _require_dict(state.get("player_contracts"), "player_contracts")
    _require_dict(state.get("active_contract_id_by_player"), "active_contract_id_by_player")
    _require_list(state.get("free_agents"), "free_agents")
    _require_dict(state.get("gm_profiles"), "gm_profiles")
    _require_dict(state.get("postseason"), "postseason")

    postseason = state.get("postseason")
    if any(key in postseason for key in _PHASE_RESULTS_KEYS):
        raise ValueError("postseason must not contain results containers")
    _validate_postseason_block(postseason, "postseason")

    cached_views = state.get("cached_views")
    meta = cached_views.get("_meta")
    _require_dict(meta, "cached_views._meta")
    if "scores" not in meta or "schedule" not in meta:
        raise ValueError("cached_views._meta must contain scores and schedule")

    scores_meta = meta.get("scores")
    schedule_meta = meta.get("schedule")
    _require_dict(scores_meta, "cached_views._meta.scores")
    _require_dict(schedule_meta, "cached_views._meta.schedule")
    if set(scores_meta.keys()) != {"built_from_turn", "season_id"}:
        raise ValueError("cached_views._meta.scores must have keys {'built_from_turn', 'season_id'}")
    if not isinstance(scores_meta.get("built_from_turn"), int) or scores_meta.get("built_from_turn") < -1:
        raise ValueError("cached_views._meta.scores.built_from_turn must be int >= -1")
    if set(schedule_meta.keys()) != {"built_from_turn_by_team"}:
        raise ValueError("cached_views._meta.schedule must have keys {'built_from_turn_by_team'}")
    _require_dict(
        schedule_meta.get("built_from_turn_by_team"),
        "cached_views._meta.schedule.built_from_turn_by_team",
    )

    league = state.get("league")
    required_league_keys = {
        "season_year",
        "season_start",
        "current_date",
        "trade_rules",
        "master_schedule",
        "db_path",
    }
    missing_league = required_league_keys - set(league.keys())
    if missing_league:
        missing = sorted(missing_league)[0]
        raise ValueError(f"league missing key {missing}")

    master_schedule = league.get("master_schedule")
    _require_dict(master_schedule, "league.master_schedule")
    by_id = master_schedule.get("by_id")
    _require_dict(by_id, "league.master_schedule.by_id")

    phase_results = state.get("phase_results")
    for phase_name, phase_data in phase_results.items():
        if phase_name not in NON_REGULAR_PHASES:
            raise ValueError(f"phase_results contains invalid phase: {phase_name}")
        _validate_phase_results_block(phase_data, f"phase_results.{phase_name}")

    season_history = state.get("season_history")
    for season_id, record in season_history.items():
        _require_dict(record, f"season_history.{season_id}")
        expected_keys = {"regular", "phases", "postseason", "archived_at_turn", "archived_at_date"}
        if set(record.keys()) != expected_keys:
            raise ValueError(f"season_history.{season_id} must have keys {expected_keys}")
        _validate_phase_results_block(record.get("regular"), f"season_history.{season_id}.regular")
        phases = record.get("phases")
        _require_dict(phases, f"season_history.{season_id}.phases")
        for phase_name, phase_data in phases.items():
            if phase_name not in NON_REGULAR_PHASES:
                raise ValueError(f"season_history.{season_id}.phases has invalid phase: {phase_name}")
            _validate_phase_results_block(
                phase_data,
                f"season_history.{season_id}.phases.{phase_name}",
            )
        _validate_postseason_block(record.get("postseason"), f"season_history.{season_id}.postseason")
        archived_at_turn = record.get("archived_at_turn")
        if not isinstance(archived_at_turn, int):
            raise ValueError(f"season_history.{season_id}.archived_at_turn must be int")
        archived_at_date = record.get("archived_at_date")
        if archived_at_date is not None and not isinstance(archived_at_date, str):
            raise ValueError(f"season_history.{season_id}.archived_at_date must be None or str")
