from __future__ import annotations

from typing import Any, Dict, List

from state_modules.state_schema import PHASES_SET, SCHEMA_VERSION


class StateValidationError(RuntimeError):
    pass


def _is_int_in_range(value: Any, min_value: int, max_value: int) -> bool:
    return isinstance(value, int) and min_value <= value <= max_value


def validate_game_state(state: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(state, dict):
        return ["state is not a dict"]

    if state.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be 3.0")

    turn = state.get("turn")
    if not isinstance(turn, int) or turn < 0:
        errors.append("turn must be non-negative int")

    required_root_keys = {
        "schema_version",
        "turn",
        "active_season_id",
        "season_history",
        "_migrations",
        "games",
        "player_stats",
        "team_stats",
        "game_results",
        "phase_containers",
        "league",
        "cached_views",
        "postseason",
        "teams",
        "players",
        "trade_agreements",
        "negotiations",
        "asset_locks",
        "trade_market",
        "trade_memory",
    }
    for key in required_root_keys:
        if key not in state:
            errors.append(f"missing root key: {key}")

    phase_containers = state.get("phase_containers")
    if isinstance(phase_containers, dict):
        if set(phase_containers.keys()) != PHASES_SET:
            errors.append("phase_containers must contain preseason, play_in, playoffs")
        else:
            for phase, container in phase_containers.items():
                if not isinstance(container, dict):
                    errors.append(f"phase_containers.{phase} must be dict")
                    continue
                for key, expected in (
                    ("games", list),
                    ("player_stats", dict),
                    ("team_stats", dict),
                    ("game_results", dict),
                ):
                    value = container.get(key)
                    if not isinstance(value, expected):
                        errors.append(
                            f"phase_containers.{phase}.{key} must be {expected.__name__}"
                        )
    elif "phase_containers" in state:
        errors.append("phase_containers must be dict")

    league = state.get("league")
    master_schedule = None
    if isinstance(league, dict):
        master_schedule = league.get("master_schedule")
    if isinstance(master_schedule, dict):
        for key, expected in (
            ("games", list),
            ("by_team", dict),
            ("by_date", dict),
            ("by_id", dict),
        ):
            value = master_schedule.get(key)
            if not isinstance(value, expected):
                errors.append(f"league.master_schedule.{key} must be {expected.__name__}")

        games = master_schedule.get("games", [])
        by_id = master_schedule.get("by_id", {})
        if isinstance(games, list) and isinstance(by_id, dict):
            for game in games:
                if not isinstance(game, dict):
                    errors.append("league.master_schedule.games entry must be dict")
                    continue
                game_id = game.get("id")
                if game_id is None:
                    game_id = game.get("game_id")
                if game_id is None:
                    errors.append("league.master_schedule.games entry missing id")
                    continue
                if game_id not in by_id:
                    errors.append(
                        f"league.master_schedule.by_id missing entry for {game_id}"
                    )
                    continue
                by_id_entry = by_id.get(game_id)
                if not isinstance(by_id_entry, dict):
                    errors.append(
                        f"league.master_schedule.by_id.{game_id} must be dict"
                    )
                    continue
                ref_id = by_id_entry.get("id")
                if ref_id is None:
                    ref_id = by_id_entry.get("game_id")
                if ref_id != game_id:
                    errors.append(
                        "league.master_schedule.by_id entry id mismatch"
                    )
    elif league is not None:
        errors.append("league.master_schedule must be dict")

    cached_views = state.get("cached_views")
    if isinstance(cached_views, dict):
        meta = cached_views.get("_meta", {})
        if isinstance(meta, dict) and isinstance(turn, int) and turn >= 0:
            for name in ("scores", "stats", "weekly_news", "playoff_news"):
                built_from_turn = meta.get(name, {}).get("built_from_turn")
                if not (
                    built_from_turn == -1 or _is_int_in_range(built_from_turn, 0, turn)
                ):
                    errors.append(
                        f"cached_views._meta.{name}.built_from_turn out of range"
                    )
            schedule_meta = meta.get("schedule", {}).get("built_from_turn_by_team")
            if isinstance(schedule_meta, dict):
                for team_id, value in schedule_meta.items():
                    if not (
                        value == -1 or _is_int_in_range(value, 0, turn)
                    ):
                        errors.append(
                            "cached_views._meta.schedule.built_from_turn_by_team "
                            f"invalid for team {team_id}"
                        )
            else:
                errors.append(
                    "cached_views._meta.schedule.built_from_turn_by_team must be dict"
                )
        else:
            errors.append("cached_views._meta must be dict")
    elif "cached_views" in state:
        errors.append("cached_views must be dict")

    return errors


def assert_valid_game_state(state: Dict[str, Any]) -> None:
    errors = validate_game_state(state)
    if errors:
        message = "State validation failed:\n" + "\n".join(errors)
        raise StateValidationError(message)
