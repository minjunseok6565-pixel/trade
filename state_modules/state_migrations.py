from __future__ import annotations

from typing import Any, Dict, List


def _backfill_ingest_turns_once(state: dict) -> None:
    """Backfill missing ingest_turn values across stored games."""
    def _iter_game_lists() -> List[List[Dict[str, Any]]]:
        lists: List[List[Dict[str, Any]]] = []
        lists.append(state["games"])
        phase_results = state["phase_results"]
        for phase in ("preseason", "play_in", "playoffs"):
            lists.append(phase_results[phase]["games"])
        for record in state["season_history"].values():
            lists.append(record["regular"]["games"])
            for phase in ("preseason", "play_in", "playoffs"):
                lists.append(record["phase_results"][phase]["games"])
        return lists

    def _valid_ingest_turn(value: Any) -> bool:
        return isinstance(value, int)

    max_turn = -1
    for games in _iter_game_lists():
        for game in games:
            ingest_turn = game.get("ingest_turn")
            if _valid_ingest_turn(ingest_turn):
                max_turn = max(max_turn, ingest_turn)

    next_turn = max_turn + 1
    for games in _iter_game_lists():
        for game in games:
            ingest_turn = game.get("ingest_turn")
            if not _valid_ingest_turn(ingest_turn):
                game["ingest_turn"] = next_turn
                next_turn += 1


def _ensure_ingest_turn_backfilled(state: dict) -> None:
    """Ensure ingest_turn backfill runs once per state instance."""
    migrations = state["_migrations"]
    if not isinstance(migrations, dict):
        raise ValueError("_migrations must be a dict")
    if migrations.get("ingest_turn_backfilled") is True:
        return
    _backfill_ingest_turns_once(state)
    migrations["ingest_turn_backfilled"] = True


def ensure_ingest_turn_backfilled_once_startup(state: dict) -> None:
    """Run ingest_turn backfill once per state instance (startup-only)."""
    _ensure_ingest_turn_backfilled(state)
