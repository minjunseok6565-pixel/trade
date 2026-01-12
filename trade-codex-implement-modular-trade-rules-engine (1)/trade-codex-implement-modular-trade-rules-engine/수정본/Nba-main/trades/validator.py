from __future__ import annotations

from datetime import date
from typing import Optional

from state import GAME_STATE, _ensure_league_state, DEFAULT_TRADE_RULES
from .errors import DEAL_INVALIDATED, TradeError
from .models import Deal
from .rules import build_trade_context, validate_all


def validate_deal(
    deal: Deal,
    current_date: Optional[date] = None,
    allow_locked_by_deal_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    if db_path is None:
        _ensure_league_state()
    else:
        league = GAME_STATE.setdefault("league", {})
        trade_rules = league.setdefault("trade_rules", {})
        for key, value in DEFAULT_TRADE_RULES.items():
            trade_rules.setdefault(key, value)

    resolved_db_path = db_path or (GAME_STATE.get("league") or {}).get("db_path")
    if not resolved_db_path:
        raise TradeError(
            DEAL_INVALIDATED,
            "db_path is required to validate trades",
        )
    ctx = build_trade_context(current_date=current_date, db_path=resolved_db_path)
    try:
        ctx.repo.validate_integrity()
        validate_all(deal, ctx)
    finally:
        # Validator closes ctx.repo to avoid SQLite connection leaks.
        repo = getattr(ctx, "repo", None)
        if repo is not None:
            repo.close()
