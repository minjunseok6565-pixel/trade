from __future__ import annotations

from datetime import date
from typing import Optional

from state import GAME_STATE, _ensure_league_state
from .models import Deal
from .rules import build_trade_context, validate_all


def validate_deal(
    deal: Deal,
    current_date: Optional[date] = None,
    allow_locked_by_deal_id: Optional[str] = None,
) -> None:
    _ensure_league_state()
    from league_repo import LeagueRepo

    db_path = (GAME_STATE.get("league") or {}).get("db_path")
    if db_path:
        with LeagueRepo(db_path) as repo:
            repo.validate_integrity()

    # RULES ENGINE CHECKS (migrated): deadline
    ctx = build_trade_context(current_date=current_date)
    validate_all(deal, ctx)
