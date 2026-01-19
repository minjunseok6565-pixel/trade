from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterator, List

from league_repo import LeagueRepo
from schema import normalize_player_id

FREE_AGENT_TEAM_ID = "FA"


def _get_db_path(game_state: dict) -> str:
    league_state = game_state.get("league") or {}
    db_path = league_state.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required")
    return str(db_path)


def _normalize_player_id(value) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))


@contextlib.contextmanager
def _open_repo(game_state: dict, repo: LeagueRepo | None) -> Iterator[LeagueRepo]:
    if repo is not None:
        yield repo
        return

    db_path = _get_db_path(game_state)
    with LeagueRepo(db_path) as managed_repo:
        managed_repo.init_db()
        yield managed_repo


def list_free_agents(
    game_state: dict,
    *,
    player_ids_only: bool = True,
    repo: LeagueRepo | None = None,
) -> List[str] | List[Dict[str, Any]]:
    """Return the current free agents from DB.

    By default returns a list of player_id strings.

    If player_ids_only=False, returns a list of small dict rows.
    (We keep the shape minimal to avoid coupling to DB schema changes.)
    """
    with _open_repo(game_state, repo) as r:
        ids = [str(pid) for pid in (r.list_free_agents(source="roster") or [])]

    if player_ids_only:
        return ids
    return [{"player_id": pid} for pid in ids]


def is_free_agent(
    game_state: dict,
    player_id: str,
    *,
    repo: LeagueRepo | None = None,
) -> bool:
    """True iff the player's active roster entry is assigned to FA in DB."""
    pid = _normalize_player_id(player_id)
    with _open_repo(game_state, repo) as r:
        try:
            team_id = r.get_team_id_by_player(pid)
        except Exception:
            return False
    return str(team_id).upper() == FREE_AGENT_TEAM_ID
