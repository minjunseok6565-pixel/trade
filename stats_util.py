from __future__ import annotations

from typing import Any, Dict, List


TRACKED_STATS = ["PTS", "AST", "REB", "3PM"]


def compute_league_leaders(player_stats: dict) -> dict:
    """Compute per-game leaders from regular-season player_stats."""
    leaders: Dict[str, List[Dict[str, Any]]] = {s: [] for s in TRACKED_STATS}

    for stat_name in TRACKED_STATS:
        rows: List[Dict[str, Any]] = []
        for entry in player_stats.values():
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

    return leaders


def compute_playoff_league_leaders(player_stats: dict) -> dict:
    """Compute per-game leaders from playoff player_stats."""
    leaders: Dict[str, List[Dict[str, Any]]] = {s: [] for s in TRACKED_STATS}

    for stat_name in TRACKED_STATS:
        rows: List[Dict[str, Any]] = []
        for entry in player_stats.values():
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

    return leaders
