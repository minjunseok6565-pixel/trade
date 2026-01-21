from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

from state_store import GAME_STATE


def normalize_player_ids(game_state: dict, *, allow_legacy_numeric: bool = True) -> dict:
    """
    Normalize GAME_STATE identifiers so *player_id is always a string* and unique.

    Why:
    - Prevents "12"(str) vs 12(int) from becoming two different players.
    - Makes it safe to join boxscore/trades/contracts/roster by the same key.

    Policy:
    - Keys of GAME_STATE["players"] are canonical player_id strings.
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
                    f"GAME_STATE.players[{raw_key!r}].player_id is invalid: {meta.get('player_id')!r}"
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
            "Duplicate player_id keys detected while normalizing GAME_STATE['players'].\n"
            f"Examples: {conflicts[:5]!r}\n"
            "Fix: migrate all IDs to a single canonical player_id (string) and remove duplicates."
        )

    if invalid:
        report["invalid_keys_count"] = len(invalid)
        report["invalid_keys"] = invalid[:10]
        raise ValueError(
            "Invalid player keys detected in GAME_STATE['players'] while normalizing.\n"
            f"Examples: {invalid[:10]!r}"
        )

    game_state["players"] = new_players

    if "free_agents" in game_state:
        fa = game_state.get("free_agents")
        if not isinstance(fa, list):
            game_state["free_agents"] = []
        else:
            out: List[str] = []
            seen: set[str] = set()
            for item in fa:
                pid = str(
                    normalize_player_id(
                        item, strict=False, allow_legacy_numeric=allow_legacy_numeric
                    )
                )
                if pid != str(item):
                    report["converted_free_agents_count"] += 1
                if pid in seen:
                    continue
                out.append(pid)
                seen.add(pid)
            game_state["free_agents"] = out

    debug = game_state.setdefault("debug", {})
    debug.setdefault("normalization", []).append(report)
    return report


def _backfill_ingest_turns_once() -> None:
    """Backfill missing ingest_turn values across stored games."""
    all_games: List[Dict[str, Any]] = []

    regular_games = GAME_STATE.get("games", [])
    if isinstance(regular_games, list):
        all_games.extend([g for g in regular_games if isinstance(g, dict)])

    postseason = GAME_STATE.get("postseason", {})
    if isinstance(postseason, dict):
        for container in postseason.values():
            if not isinstance(container, dict):
                continue
            games = container.get("games", [])
            if isinstance(games, list):
                all_games.extend([g for g in games if isinstance(g, dict)])

    season_history = GAME_STATE.get("season_history", {})
    if isinstance(season_history, dict):
        for record in season_history.values():
            if not isinstance(record, dict):
                continue
            games = record.get("games", [])
            if isinstance(games, list):
                all_games.extend([g for g in games if isinstance(g, dict)])

    used_turns = {
        int(game["ingest_turn"])
        for game in all_games
        if isinstance(game.get("ingest_turn"), int)
        and not isinstance(game.get("ingest_turn"), bool)
        and int(game.get("ingest_turn")) > 0
    }

    missing_games = [
        game
        for game in all_games
        if not (
            isinstance(game.get("ingest_turn"), int)
            and not isinstance(game.get("ingest_turn"), bool)
            and int(game.get("ingest_turn")) > 0
        )
    ]

    def _missing_sort_key(game: Dict[str, Any]) -> tuple[date, str, str, str]:
        raw_date = game.get("date")
        try:
            parsed_date = date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            parsed_date = date.min
        return (
            parsed_date,
            str(game.get("season_id") or ""),
            str(game.get("phase") or ""),
            str(game.get("game_id") or ""),
        )

    missing_games.sort(key=_missing_sort_key)

    next_turn = 1
    for game in missing_games:
        while next_turn in used_turns:
            next_turn += 1
        game["ingest_turn"] = next_turn
        used_turns.add(next_turn)
        next_turn += 1

    max_turn = max(used_turns) if used_turns else 0
    if int(GAME_STATE.get("turn", 0) or 0) < max_turn:
        GAME_STATE["turn"] = max_turn


def _ensure_ingest_turn_backfilled() -> None:
    """Ensure ingest_turn backfill runs once per GAME_STATE instance."""
    migrations = GAME_STATE.setdefault("_migrations", {})
    if migrations.get("ingest_turn_backfilled") is True:
        return
    _backfill_ingest_turns_once()
    migrations["ingest_turn_backfilled"] = True


def ensure_ingest_turn_backfilled_once_startup() -> None:
    """Run ingest_turn backfill once per GAME_STATE instance (startup-only)."""
    _ensure_ingest_turn_backfilled()
