from __future__ import annotations

import os
import contextlib
from typing import List

from .errors import TradeError, PICK_NOT_OWNED


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
 

def transfer_pick(game_state: dict, pick_id: str, from_team: str, to_team: str) -> None:
    """
    Deprecated for normal trade flow (execute_trade should move picks).
    Kept for legacy callers: ownership-check then DB update via Repo.
    """
    from league_repo import LeagueRepo
    db_path = _get_db_path_from_game_state(game_state)
    pid = str(pick_id)
    f = str(from_team).upper()
    t = str(to_team).upper()
    with LeagueRepo(db_path) as repo:
        repo.init_db()
        with repo.transaction() as cur:
            row = cur.execute("SELECT owner_team FROM draft_picks WHERE pick_id=?;", (pid,)).fetchone()
            if not row:
                raise TradeError(PICK_NOT_OWNED, "Pick not found", {"pick_id": pid})
            owner = str(row["owner_team"]).upper()
            if owner != f:
                raise TradeError(PICK_NOT_OWNED, "Pick not owned by team", {"pick_id": pid, "team_id": f, "owner_team": owner})
            cur.execute("UPDATE draft_picks SET owner_team=? WHERE pick_id=?;", (t, pid))
