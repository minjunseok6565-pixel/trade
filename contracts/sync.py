"""DB -> GAME_STATE cache refresh helpers (SSOT = DB).

After migration, contract ledgers are no longer stored in GAME_STATE:
  - contracts / player_contracts / active_contract_id_by_player / free_agents

This module must NOT create or depend on those legacy keys.
Its only responsibility (if needed) is to refresh lightweight cached fields
inside GAME_STATE["players"] from the DB roster.
"""

from __future__ import annotations

import contextlib
from typing import Dict, Iterator, List, Optional

from league_repo import LeagueRepo
from schema import normalize_player_id


def _get_db_path(game_state: dict) -> str:
    league_state = game_state.get("league") or {}
    db_path = league_state.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required")
    return str(db_path)


@contextlib.contextmanager
def _open_repo(game_state: dict, repo: LeagueRepo | None) -> Iterator[LeagueRepo]:
    if repo is not None:
        yield repo
        return

    db_path = _get_db_path(game_state)
    with LeagueRepo(db_path) as managed_repo:
        managed_repo.init_db()
        yield managed_repo


def refresh_player_cache_from_db(
    game_state: dict,
    *,
    player_ids: Optional[List[str]] = None,
    repo: LeagueRepo | None = None,
) -> Dict[str, int]:
    """Refresh GAME_STATE["players"] cache from DB roster.

    Updates (best-effort) cached player dict fields:
      - team_id  (from roster.team_id)
      - salary   (float, from roster.salary_amount)

    Does NOT create missing cache entries. DB remains SSOT.
    Returns a small summary dict: {"db_rows":..., "updated":..., "missing_in_cache":..., "not_found_in_db":...}
    """
    players_cache = game_state.get("players")
    if not isinstance(players_cache, dict):
        # No cache to refresh; still validate DB access path.
        with _open_repo(game_state, repo) as r:
            _ = r  # touch
        return {"db_rows": 0, "updated": 0, "missing_in_cache": 0, "not_found_in_db": 0}
 
    normalized_ids: List[str] = []
    if player_ids:
        seen = set()
        for pid in player_ids:
            try:
                nid = str(normalize_player_id(pid, strict=False, allow_legacy_numeric=True))
            except Exception:
                continue
            if nid in seen:
                continue
            seen.add(nid)
            normalized_ids.append(nid)


    rows = []
    with _open_repo(game_state, repo) as r:
        if normalized_ids:
            placeholders = ",".join(["?"] * len(normalized_ids))
            sql = (
                f"SELECT player_id, team_id, salary_amount "
                f"FROM roster WHERE status='active' AND player_id IN ({placeholders});"
            )
            with r.transaction() as cur:
                rows = cur.execute(sql, tuple(normalized_ids)).fetchall()
        else:
            with r.transaction() as cur:
                rows = cur.execute(
                    "SELECT player_id, team_id, salary_amount FROM roster WHERE status='active';"
                ).fetchall()

    db_rows = len(rows)
    updated = 0
    missing_in_cache = 0

    for row in rows:
        pid = str(row["player_id"])
        team_id = str(row["team_id"]).upper() if row["team_id"] is not None else ""
        salary_amount = row["salary_amount"]
        salary_f = float(salary_amount or 0.0)

        cached = players_cache.get(pid)
        if not isinstance(cached, dict):
            missing_in_cache += 1
            continue

        cached["team_id"] = team_id
        cached["salary"] = salary_f
        updated += 1
 
    not_found_in_db = 0
    if normalized_ids:
        found = {str(r["player_id"]) for r in rows}
        not_found_in_db = sum(1 for pid in normalized_ids if pid not in found)


    return {
        "db_rows": db_rows,
        "updated": updated,
        "missing_in_cache": missing_in_cache,
        "not_found_in_db": not_found_in_db,
    }
