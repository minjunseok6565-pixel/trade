from __future__ import annotations

from typing import Any, Dict, List

import state as state_facade


TRACKED_STATS = ["PTS", "AST", "REB", "3PM"]


def compute_league_leaders() -> Dict[str, List[Dict[str, Any]]]:
    """player_stats 기반으로 per game 리그 리더 상위 5명을 계산한다."""
    state = state_facade.export_state()
    season_stats = state.get("player_stats") or {}
    leaders: Dict[str, List[Dict[str, Any]]] = {s: [] for s in TRACKED_STATS}

    for stat_name in TRACKED_STATS:
        rows: List[Dict[str, Any]] = []
        for entry in season_stats.values():
            games = entry.get("games", 0) or 0
            if games <= 0:
                continue
            totals = entry.get("totals", {}) or {}
            try:
                per_game = float(totals.get(stat_name, 0.0)) / games
            except (TypeError, ValueError, ZeroDivisionError):
                per_game = 0.0
            rows.append(
                {
                    "player_id": entry.get("player_id"),
                    "name": entry.get("name"),
                    "team_id": entry.get("team_id"),
                    "games": games,
                    "GP": games,
                    "per_game": per_game,
                    stat_name: per_game,
                }
            )

        rows_sorted = sorted(rows, key=lambda r: r.get("per_game", 0), reverse=True)
        leaders[stat_name] = rows_sorted[:5]

    cached_views = state.get("cached_views", {})
    stats_cache = cached_views.get("stats")
    if isinstance(stats_cache, dict):
        stats_cache["leaders"] = leaders
        state_facade.import_state(state)
    return leaders


def compute_playoff_league_leaders() -> Dict[str, List[Dict[str, Any]]]:
    state = state_facade.export_state()
    postseason = state.get("postseason") or {}
    playoff_stats = postseason.get("playoff_player_stats") or {}
    leaders: Dict[str, List[Dict[str, Any]]] = {s: [] for s in TRACKED_STATS}

    for stat_name in TRACKED_STATS:
        rows: List[Dict[str, Any]] = []
        for entry in playoff_stats.values():
            games = entry.get("games", 0) or 0
            if games <= 0:
                continue
            totals = entry.get("totals", {}) or {}
            try:
                per_game = float(totals.get(stat_name, 0.0)) / games
            except (TypeError, ValueError, ZeroDivisionError):
                per_game = 0.0
            rows.append(
                {
                    "player_id": entry.get("player_id"),
                    "name": entry.get("name"),
                    "team_id": entry.get("team_id"),
                    "games": games,
                    "GP": games,
                    "per_game": per_game,
                    stat_name: per_game,
                }
            )

        rows_sorted = sorted(rows, key=lambda r: r.get("per_game", 0), reverse=True)
        leaders[stat_name] = rows_sorted[:5]

    cached_views = state.get("cached_views", {})
    stats_cache = cached_views.get("stats")
    if isinstance(stats_cache, dict):
        stats_cache["playoff_leaders"] = leaders
        state_facade.import_state(state)
    return leaders
