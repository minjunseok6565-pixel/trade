from __future__ import annotations

import os
import contextlib
from typing import List

def _get_db_path_from_game_state(game_state: dict) -> str:
    league = game_state.get("league", {}) if isinstance(game_state, dict) else {}
    if isinstance(league, dict):
        db_path = league.get("db_path")
        if db_path:
            return str(db_path)
    return os.environ.get("LEAGUE_DB_PATH", "league.db")


@contextlib.contextmanager
def _open_service(db_path: str):
    from league_service import LeagueService
    svc_or_cm = LeagueService.open(db_path)
    if hasattr(svc_or_cm, "__enter__"):
        with svc_or_cm as svc:
            yield svc
        return
    svc = svc_or_cm
    try:
        yield svc
    finally:
        close = getattr(svc, "close", None)
        if callable(close):
            close()


def init_draft_picks_if_needed(
    game_state: dict,
    draft_year: int,
    all_team_ids: List[str],
    years_ahead: int = 7,
) -> None:
    """
    ✅ DB-SSOT seed (idempotent).
    draft_picks state ledger has been migrated away; ensure presence in DB instead.
    """
    db_path = _get_db_path_from_game_state(game_state)
    team_ids = [str(t).upper() for t in (all_team_ids or [])]
    with _open_service(db_path) as svc:
        svc.ensure_draft_picks_seeded(int(draft_year), team_ids, years_ahead=int(years_ahead))
 
