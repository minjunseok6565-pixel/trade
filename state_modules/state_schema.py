from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

SCHEMA_VERSION = "3.0"

PHASES = ("preseason", "play_in", "playoffs")
PHASES_SET = {"preseason", "play_in", "playoffs"}
REGULAR_PHASE_NAME = "regular"


def default_phase_container() -> Dict[str, Any]:
    return {
        "games": [],
        "player_stats": {},
        "team_stats": {},
        "game_results": {},
    }


def default_postseason_state() -> Dict[str, Any]:
    return {
        "field": None,
        "play_in": None,
        "playoffs": None,
        "champion": None,
        "my_team_id": None,
        "playoff_player_stats": {},
    }


def default_cached_views_state() -> Dict[str, Any]:
    return {
        "scores": {"latest_date": None, "games": []},
        "schedule": {"teams": {}},
        "stats": {"leaders": None, "playoff_leaders": None},
        "weekly_news": {"last_generated_week_start": None, "items": []},
        "playoff_news": {"series_game_counts": {}, "items": []},
        "_meta": {
            "scores": {"built_from_turn": -1, "season_id": None},
            "schedule": {"built_from_turn_by_team": {}, "season_id": None},
            "stats": {"built_from_turn": -1, "season_id": None},
            "weekly_news": {"built_from_turn": -1, "season_id": None},
            "playoff_news": {"built_from_turn": -1, "season_id": None},
        },
    }


def default_league_state(db_path: str) -> Dict[str, Any]:
    from state_modules.state_store import DEFAULT_TRADE_RULES

    return {
        "season_year": None,
        "draft_year": None,
        "season_start": None,
        "current_date": None,
        "last_gm_tick_date": None,
        "db_path": db_path,
        "trade_rules": {**DEFAULT_TRADE_RULES},
        "master_schedule": {
            "games": [],
            "by_team": {},
            "by_date": {},
            "by_id": {},
        },
    }


def build_default_state_v3(
    db_path: str,
    default_trade_market: Dict[str, Any],
    default_trade_memory: Dict[str, Any],
    default_trade_rules: Dict[str, Any],
) -> Dict[str, Any]:
    phase_containers = {
        "preseason": default_phase_container(),
        "play_in": default_phase_container(),
        "playoffs": default_phase_container(),
    }
    state = {
        "schema_version": SCHEMA_VERSION,
        "turn": 0,
        "active_season_id": None,
        "season_history": {},
        "_migrations": {},
        "games": [],
        "player_stats": {},
        "team_stats": {},
        "game_results": {},
        "phase_containers": phase_containers,
        "league": default_league_state(db_path),
        "cached_views": default_cached_views_state(),
        "postseason": default_postseason_state(),
        "teams": {},
        "players": {},
        "trade_agreements": {},
        "negotiations": {},
        "asset_locks": {},
        "trade_market": deepcopy(default_trade_market),
        "trade_memory": deepcopy(default_trade_memory),
    }

    if default_trade_rules:
        state.setdefault("league", {})["trade_rules"] = {
            **default_trade_rules,
        }

    return state
