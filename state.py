from __future__ import annotations

import logging
import os
from copy import deepcopy
from typing import Any, Dict

from state_modules.state_migrations import migrate_to_latest
from state_modules.state_schema import (
    PHASES,
    PHASES_SET,
    default_cached_views_state,
    default_phase_container,
    default_postseason_state,
)
from state_modules.state_store import (
    DEFAULT_TRADE_RULES,
    _DEFAULT_TRADE_MARKET,
    _DEFAULT_TRADE_MEMORY,
    get_state_ref,
    replace_state,
)
from state_modules.state_validate import assert_valid_game_state, validate_game_state


def _state() -> Dict[str, Any]:
    return get_state_ref()


def init_state(*, validate_mode: str = "raise") -> None:
    state = _state()
    migrated_state = migrate_to_latest(
        state,
        db_path=os.environ.get("LEAGUE_DB_PATH") or "league.db",
        default_trade_market=_DEFAULT_TRADE_MARKET,
        default_trade_memory=_DEFAULT_TRADE_MEMORY,
        default_trade_rules=DEFAULT_TRADE_RULES,
    )
    replace_state(migrated_state)

    if validate_mode == "raise":
        assert_valid_game_state(migrated_state)
    elif validate_mode == "warn":
        errors = validate_game_state(migrated_state)
        for error in errors:
            logging.warning("State validation warning: %s", error)
    elif validate_mode == "off":
        return
    else:
        raise ValueError(f"Unknown validate_mode: {validate_mode}")


def export_state(*, redact: bool = False) -> Dict[str, Any]:
    payload = deepcopy(_state())
    if redact:
        payload.pop("players", None)
        payload.pop("teams", None)
    return payload


def import_state(payload: Dict[str, Any], *, validate_mode: str = "raise") -> None:
    replace_state(payload)
    init_state(validate_mode=validate_mode)
    if validate_mode == "raise":
        assert_valid_game_state(_state())


def get_turn() -> int:
    return _state()["turn"]


def bump_turn(*, reason: str) -> int:
    s = _state()
    s["turn"] += 1
    migrations = s.setdefault("_migrations", {})
    migrations["last_turn_bump_reason"] = reason
    migrations["last_turn_bump_turn"] = s["turn"]
    return s["turn"]


def get_active_season_id() -> str | None:
    return _state().get("active_season_id")


def ensure_active_season_id(season_id: str) -> None:
    if _state().get("active_season_id") != season_id:
        rollover_season(season_id)


def rollover_season(next_season_id: str) -> None:
    s = _state()
    old_id = s.get("active_season_id")
    if old_id is not None:
        s.setdefault("season_history", {})[old_id] = {
            "games": deepcopy(s.get("games", [])),
            "player_stats": deepcopy(s.get("player_stats", {})),
            "team_stats": deepcopy(s.get("team_stats", {})),
            "game_results": deepcopy(s.get("game_results", {})),
        }

    s["games"] = []
    s["player_stats"] = {}
    s["team_stats"] = {}
    s["game_results"] = {}

    s["phase_containers"] = {
        phase: default_phase_container() for phase in PHASES
    }

    reset_postseason_state()
    s["cached_views"] = default_cached_views_state()
    s["active_season_id"] = next_season_id
    assert_valid_game_state(s)


def get_current_date() -> str | None:
    return _state().get("league", {}).get("current_date")


def set_current_date(date_str: str | None) -> None:
    _state().setdefault("league", {})["current_date"] = date_str


def get_db_path() -> str:
    return _state().get("league", {}).get("db_path", "")


def set_db_path(db_path: str) -> None:
    _state().setdefault("league", {})["db_path"] = db_path


def ensure_trade_blocks() -> None:
    s = _state()
    if not isinstance(s.get("trade_market"), dict):
        s["trade_market"] = deepcopy(_DEFAULT_TRADE_MARKET)
    if not isinstance(s.get("trade_memory"), dict):
        s["trade_memory"] = deepcopy(_DEFAULT_TRADE_MEMORY)
    if not isinstance(s.get("trade_agreements"), dict):
        s["trade_agreements"] = {}
    if not isinstance(s.get("negotiations"), dict):
        s["negotiations"] = {}
    if not isinstance(s.get("asset_locks"), dict):
        s["asset_locks"] = {}


def initialize_master_schedule_if_needed(*, force: bool = False) -> None:
    master_schedule = get_master_schedule()
    if force or not master_schedule.get("games"):
        from state_modules.state_schedule import (
            initialize_master_schedule_if_needed as _initialize,
        )

        _initialize()
    rebuild_master_schedule_by_id()
    assert_valid_game_state(_state())


def get_master_schedule() -> Dict[str, Any]:
    return _state().setdefault("league", {}).setdefault("master_schedule", {})


def rebuild_master_schedule_by_id() -> None:
    master_schedule = get_master_schedule()
    games = master_schedule.get("games", [])
    by_id: Dict[str, Any] = {}
    if isinstance(games, list):
        for game in games:
            if not isinstance(game, dict):
                continue
            game_id = game.get("id") or game.get("game_id")
            if game_id is None:
                continue
            by_id[game_id] = game
    master_schedule["by_id"] = by_id


def mark_cache_dirty_scores() -> None:
    meta = _state().setdefault("cached_views", {}).setdefault("_meta", {})
    meta.setdefault("scores", {})["built_from_turn"] = -1


def mark_cache_dirty_stats() -> None:
    meta = _state().setdefault("cached_views", {}).setdefault("_meta", {})
    meta.setdefault("stats", {})["built_from_turn"] = -1


def mark_cache_dirty_weekly_news() -> None:
    meta = _state().setdefault("cached_views", {}).setdefault("_meta", {})
    meta.setdefault("weekly_news", {})["built_from_turn"] = -1


def mark_cache_dirty_playoff_news() -> None:
    meta = _state().setdefault("cached_views", {}).setdefault("_meta", {})
    meta.setdefault("playoff_news", {})["built_from_turn"] = -1


def mark_cache_dirty_schedule_for_teams(team_ids: list[str]) -> None:
    meta = _state().setdefault("cached_views", {}).setdefault("_meta", {})
    schedule_meta = meta.setdefault("schedule", {}).setdefault(
        "built_from_turn_by_team", {}
    )
    for team_id in team_ids:
        schedule_meta[team_id] = -1


def ingest_game_result(
    game_result: Dict[str, Any],
    *,
    phase: str = "regular",
    store_raw_result: bool = True,
) -> Dict[str, Any]:
    if phase != "regular" and phase not in PHASES_SET:
        raise ValueError(f"Invalid phase: {phase}")

    s = _state()
    container = s if phase == "regular" else s["phase_containers"][phase]

    game_id = game_result.get("id")
    if game_id is None:
        game_id = game_result.get("game_id")
    if game_id is None:
        raise ValueError("game_result missing id")

    compact_entry = {
        "id": game_id,
        "home_team_id": game_result.get("home_team_id"),
        "away_team_id": game_result.get("away_team_id"),
        "date": game_result.get("date"),
        "home_score": game_result.get("home_score"),
        "away_score": game_result.get("away_score"),
    }
    container.setdefault("games", []).append(compact_entry)

    if store_raw_result:
        container.setdefault("game_results", {})[game_id] = game_result

    bump_turn(reason=f"ingest_game_result:{phase}:{game_id}")
    mark_cache_dirty_scores()
    mark_cache_dirty_stats()

    home_team_id = compact_entry.get("home_team_id")
    away_team_id = compact_entry.get("away_team_id")
    if home_team_id and away_team_id:
        mark_cache_dirty_schedule_for_teams([home_team_id, away_team_id])

    assert_valid_game_state(s)
    return game_result


def reset_postseason_state() -> None:
    _state()["postseason"] = default_postseason_state()


def set_postseason_brackets(
    field: Any,
    play_in: Any,
    playoffs: Any,
    *,
    champion: Any = None,
    my_team_id: Any = None,
) -> None:
    postseason = _state().get("postseason")
    if not isinstance(postseason, dict):
        postseason = default_postseason_state()
        _state()["postseason"] = postseason
    postseason["field"] = field
    postseason["play_in"] = play_in
    postseason["playoffs"] = playoffs
    postseason["champion"] = champion
    postseason["my_team_id"] = my_team_id
    assert_valid_game_state(_state())


def set_rosters(teams: Dict[str, Any], players: Dict[str, Any]) -> None:
    s = _state()
    s["teams"] = teams
    s["players"] = players
    assert_valid_game_state(s)


__all__ = [
    "init_state",
    "export_state",
    "import_state",
    "get_turn",
    "bump_turn",
    "get_active_season_id",
    "ensure_active_season_id",
    "rollover_season",
    "get_current_date",
    "set_current_date",
    "get_db_path",
    "set_db_path",
    "ensure_trade_blocks",
    "initialize_master_schedule_if_needed",
    "get_master_schedule",
    "rebuild_master_schedule_by_id",
    "mark_cache_dirty_scores",
    "mark_cache_dirty_stats",
    "mark_cache_dirty_weekly_news",
    "mark_cache_dirty_playoff_news",
    "mark_cache_dirty_schedule_for_teams",
    "ingest_game_result",
    "reset_postseason_state",
    "set_postseason_brackets",
    "set_rosters",
]
