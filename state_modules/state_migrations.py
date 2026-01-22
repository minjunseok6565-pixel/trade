from __future__ import annotations

from typing import Any, Dict

from state_modules.state_schema import (
    PHASES,
    SCHEMA_VERSION,
    build_default_state_v3,
    default_cached_views_state,
    default_league_state,
    default_phase_container,
    default_postseason_state,
)

LATEST_SCHEMA_VERSION = SCHEMA_VERSION


def _ensure_postseason_keys(postseason: Dict[str, Any]) -> None:
    defaults = default_postseason_state()
    for key, value in defaults.items():
        postseason.setdefault(key, value)


def _ensure_cached_views(state: Dict[str, Any]) -> None:
    if not isinstance(state.get("cached_views"), dict):
        state["cached_views"] = default_cached_views_state()
        return

    cached_views = state["cached_views"]
    meta = cached_views.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        cached_views["_meta"] = meta

    defaults = default_cached_views_state()
    for key, value in defaults.items():
        cached_views.setdefault(key, value if key != "_meta" else meta)

    default_meta = defaults["_meta"]
    for key, value in default_meta.items():
        meta.setdefault(key, value)


def _ensure_phase_containers(state: Dict[str, Any]) -> None:
    phase_containers = state.get("phase_containers")
    if not isinstance(phase_containers, dict):
        phase_containers = {}
        state["phase_containers"] = phase_containers

    for phase in PHASES:
        phase_containers.setdefault(phase, default_phase_container())

    postseason = state.get("postseason")
    if isinstance(postseason, dict):
        for phase in PHASES:
            candidate = postseason.get(phase)
            if (
                isinstance(candidate, dict)
                and {"games", "player_stats", "team_stats", "game_results"}
                <= set(candidate.keys())
            ):
                phase_containers[phase] = candidate
                del postseason[phase]


def _ensure_master_schedule(state: Dict[str, Any]) -> None:
    league = state.get("league")
    if not isinstance(league, dict):
        league = default_league_state(state.get("db_path", ""))
        state["league"] = league

    master_schedule = league.get("master_schedule")
    if not isinstance(master_schedule, dict):
        master_schedule = {
            "games": [],
            "by_team": {},
            "by_date": {},
            "by_id": {},
        }
        league["master_schedule"] = master_schedule

    master_schedule.setdefault("games", [])
    master_schedule.setdefault("by_team", {})
    master_schedule.setdefault("by_date", {})
    by_id = master_schedule.setdefault("by_id", {})

    games = master_schedule.get("games", [])
    if isinstance(games, list):
        for game in games:
            if not isinstance(game, dict):
                continue
            game_id = game.get("id") or game.get("game_id")
            if game_id is None:
                continue
            if game_id not in by_id:
                by_id[game_id] = game


def migrate_to_latest(
    state: Dict[str, Any] | None,
    *,
    db_path: str,
    default_trade_market: Dict[str, Any],
    default_trade_memory: Dict[str, Any],
    default_trade_rules: Dict[str, Any],
) -> Dict[str, Any]:
    if not state:
        return build_default_state_v3(
            db_path,
            default_trade_market,
            default_trade_memory,
            default_trade_rules,
        )

    if state.get("schema_version") != SCHEMA_VERSION:
        defaults = build_default_state_v3(
            db_path,
            default_trade_market,
            default_trade_memory,
            default_trade_rules,
        )
        for key, value in defaults.items():
            state.setdefault(key, value)

        _ensure_master_schedule(state)
        _ensure_cached_views(state)
        _ensure_phase_containers(state)

        postseason = state.get("postseason")
        if not isinstance(postseason, dict):
            postseason = default_postseason_state()
            state["postseason"] = postseason
        _ensure_postseason_keys(postseason)

        state["schema_version"] = SCHEMA_VERSION

    return state


def normalize_player_ids(state: Dict[str, Any], *, allow_legacy_numeric: bool = True) -> Dict[str, Any]:
    _ = allow_legacy_numeric
    return state
