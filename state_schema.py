from __future__ import annotations

from typing import Any, Dict

GAME_STATE_SCHEMA_VERSION = "3.0"

ALLOWED_PHASES = {"regular", "play_in", "playoffs", "preseason"}
NON_REGULAR_PHASES = {"play_in", "playoffs", "preseason"}


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
        "stats": {
            "leaders": None,
            "playoff_leaders": None,
        },
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
        "postseason": create_default_postseason_state(),
    }


ALLOWED_TOP_LEVEL_KEYS = set(create_default_state().keys())


def _validate_phase_results_block(block: Dict[str, Any], label: str) -> None:
    expected_keys = {"games", "player_stats", "team_stats", "game_results"}
    if set(block.keys()) != expected_keys:
        raise ValueError(f"{label} must have keys {expected_keys}")
    if not isinstance(block["games"], list):
        raise ValueError(f"{label} games must be a list")
    if not isinstance(block["player_stats"], dict):
        raise ValueError(f"{label} player_stats must be a dict")
    if not isinstance(block["team_stats"], dict):
        raise ValueError(f"{label} team_stats must be a dict")
    if not isinstance(block["game_results"], dict):
        raise ValueError(f"{label} game_results must be a dict")


def _validate_postseason_block(block: Dict[str, Any], label: str) -> None:
    forbidden_keys = {"games", "player_stats", "team_stats", "game_results"}
    if forbidden_keys.intersection(block.keys()):
        raise ValueError("postseason must not contain results containers")
    expected_keys = {"field", "play_in", "playoffs", "champion", "my_team_id"}
    if set(block.keys()) != expected_keys:
        raise ValueError(f"{label} must have keys {expected_keys}")


def validate_game_state(state: dict) -> None:
    for key in state.keys():
        if key not in ALLOWED_TOP_LEVEL_KEYS:
            raise ValueError(f"Unknown top-level key: {key}")

    if state.get("schema_version") != GAME_STATE_SCHEMA_VERSION:
        raise ValueError("schema_version mismatch")

    turn = state.get("turn")
    if not isinstance(turn, int) or turn < 0:
        raise ValueError("turn must be a non-negative int")

    active_season_id = state.get("active_season_id")
    if active_season_id is not None and not isinstance(active_season_id, str):
        raise ValueError("active_season_id must be None or str")

    for key in (
        "season_history",
        "player_stats",
        "team_stats",
        "game_results",
        "phase_results",
        "cached_views",
        "league",
        "teams",
        "players",
        "trade_agreements",
        "negotiations",
        "asset_locks",
        "trade_market",
        "trade_memory",
        "postseason",
    ):
        if not isinstance(state.get(key), dict):
            raise ValueError(f"{key} must be a dict")

    if not isinstance(state.get("games"), list):
        raise ValueError("games must be a list")

    postseason = state["postseason"]
    _validate_postseason_block(postseason, "postseason")

    cached_views = state["cached_views"]
    meta = cached_views.get("_meta")
    if not isinstance(meta, dict):
        raise ValueError("cached_views._meta must be a dict")
    if "scores" not in meta or "schedule" not in meta:
        raise ValueError("cached_views._meta must include scores and schedule")

    scores_meta = meta["scores"]
    if not isinstance(scores_meta, dict):
        raise ValueError("cached_views._meta.scores must be a dict")
    if set(scores_meta.keys()) != {"built_from_turn", "season_id"}:
        raise ValueError("cached_views._meta.scores must have keys built_from_turn and season_id")
    built_from_turn = scores_meta.get("built_from_turn")
    if not isinstance(built_from_turn, int) or built_from_turn < -1:
        raise ValueError("cached_views._meta.scores.built_from_turn must be int >= -1")

    schedule_meta = meta["schedule"]
    if not isinstance(schedule_meta, dict):
        raise ValueError("cached_views._meta.schedule must be a dict")
    if set(schedule_meta.keys()) != {"built_from_turn_by_team"}:
        raise ValueError("cached_views._meta.schedule must have key built_from_turn_by_team")
    if not isinstance(schedule_meta.get("built_from_turn_by_team"), dict):
        raise ValueError("cached_views._meta.schedule.built_from_turn_by_team must be a dict")

    league = state["league"]
    required_league_keys = {
        "season_year",
        "season_start",
        "current_date",
        "trade_rules",
        "master_schedule",
        "db_path",
    }
    if not required_league_keys.issubset(league.keys()):
        missing = required_league_keys - set(league.keys())
        raise ValueError(f"league missing keys: {sorted(missing)}")

    master_schedule = league.get("master_schedule")
    if not isinstance(master_schedule, dict):
        raise ValueError("league.master_schedule must be a dict")
    if not isinstance(master_schedule.get("by_id"), dict):
        raise ValueError("league.master_schedule.by_id must be a dict")

    phase_results = state["phase_results"]
    for phase, block in phase_results.items():
        if phase not in NON_REGULAR_PHASES:
            raise ValueError(f"phase_results has invalid phase: {phase}")
        if not isinstance(block, dict):
            raise ValueError(f"phase_results.{phase} must be a dict")
        _validate_phase_results_block(block, f"phase_results.{phase}")

    season_history = state["season_history"]
    for season_id, record in season_history.items():
        if not isinstance(record, dict):
            raise ValueError(f"season_history.{season_id} must be a dict")
        expected_keys = {"regular", "phases", "postseason", "archived_at_turn", "archived_at_date"}
        if set(record.keys()) != expected_keys:
            raise ValueError(f"season_history.{season_id} must have keys {expected_keys}")

        regular = record["regular"]
        if not isinstance(regular, dict):
            raise ValueError(f"season_history.{season_id}.regular must be a dict")
        _validate_phase_results_block(regular, f"season_history.{season_id}.regular")

        phases = record["phases"]
        if not isinstance(phases, dict):
            raise ValueError(f"season_history.{season_id}.phases must be a dict")
        for phase, block in phases.items():
            if phase not in NON_REGULAR_PHASES:
                raise ValueError(f"season_history.{season_id}.phases has invalid phase: {phase}")
            if not isinstance(block, dict):
                raise ValueError(f"season_history.{season_id}.phases.{phase} must be a dict")
            _validate_phase_results_block(block, f"season_history.{season_id}.phases.{phase}")

        postseason_block = record["postseason"]
        if not isinstance(postseason_block, dict):
            raise ValueError(f"season_history.{season_id}.postseason must be a dict")
        _validate_postseason_block(postseason_block, f"season_history.{season_id}.postseason")

        archived_at_turn = record["archived_at_turn"]
        if not isinstance(archived_at_turn, int):
            raise ValueError(f"season_history.{season_id}.archived_at_turn must be int")

        archived_at_date = record["archived_at_date"]
        if archived_at_date is not None and not isinstance(archived_at_date, str):
            raise ValueError(f"season_history.{season_id}.archived_at_date must be None or str")
