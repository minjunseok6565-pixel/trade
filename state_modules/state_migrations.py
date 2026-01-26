from __future__ import annotations

from typing import Any, Dict, List


def normalize_player_ids(game_state: dict, *, allow_legacy_numeric: bool = True) -> dict:
    """
    Normalize state identifiers so *player_id is always a string* and unique.

    Why:
    - Prevents "12"(str) vs 12(int) from becoming two different players.
    - Makes it safe to join boxscore/trades/contracts/roster by the same key.

    Policy:
    - Keys of state["players"] are canonical player_id strings.
    - player_meta["player_id"] must exist and must match the dict key.
    - (Optional) if free_agents list exists, its items are canonical player_id strings.
    """
    try:
        from schema import normalize_player_id, normalize_team_id
    except Exception as e:
        raise ImportError(f"schema.py is required for ID normalization: {e}")

    report = {
        "converted_player_keys_count": 0,
        "converted_free_agents_count": 0,
        "conflicts_count": 0,
        "invalid_keys_count": 0,
    }
    players = game_state.get("players")
    if not isinstance(players, dict):
        return report

    new_players: Dict[str, Any] = {}
    key_sources: Dict[str, Any] = {}
    conflicts: List[Dict[str, Any]] = []
    invalid: List[Any] = []

    for raw_key, raw_meta in players.items():
        try:
            pid = str(
                normalize_player_id(
                    raw_key, strict=False, allow_legacy_numeric=allow_legacy_numeric
                )
            )
        except Exception:
            invalid.append(raw_key)
            continue

        if pid != str(raw_key):
            report["converted_player_keys_count"] += 1

        meta = raw_meta if isinstance(raw_meta, dict) else {"value": raw_meta}
        if "player_id" not in meta:
            meta["player_id"] = pid
        else:
            try:
                meta_pid = str(
                    normalize_player_id(
                        meta.get("player_id"),
                        strict=False,
                        allow_legacy_numeric=allow_legacy_numeric,
                    )
                )
            except Exception:
                raise ValueError(
                    f"state.players[{raw_key!r}].player_id is invalid: {meta.get('player_id')!r}"
                )
            if meta_pid != pid:
                raise ValueError(
                    "Inconsistent player_id: dict key and meta disagree: "
                    f"key={raw_key!r} -> {pid}, meta.player_id={meta.get('player_id')!r} -> {meta_pid}"
                )
            meta["player_id"] = meta_pid

        if "team_id" in meta and meta.get("team_id") not in (None, ""):
            try:
                meta["team_id"] = str(normalize_team_id(meta["team_id"], strict=False))
            except Exception:
                meta.setdefault("_warnings", []).append(
                    f"team_id not normalized: {meta.get('team_id')!r}"
                )

        if pid in new_players:
            conflicts.append(
                {"player_id": pid, "first_key": key_sources[pid], "dup_key": raw_key}
            )
            continue

        new_players[pid] = meta
        key_sources[pid] = raw_key

    if conflicts:
        report["conflicts_count"] = len(conflicts)
        report["conflicts"] = conflicts[:10]
        raise ValueError(
            "Duplicate player_id keys detected while normalizing state['players'].\n"
            f"Examples: {conflicts[:5]!r}\n"
            "Fix: migrate all IDs to a single canonical player_id (string) and remove duplicates."
        )

    if invalid:
        report["invalid_keys_count"] = len(invalid)
        report["invalid_keys"] = invalid[:10]
        raise ValueError(
            "Invalid player keys detected in state['players'] while normalizing.\n"
            f"Examples: {invalid[:10]!r}"
        )

    game_state["players"] = new_players
    return report


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
