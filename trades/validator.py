from __future__ import annotations

from datetime import date
from typing import Optional, TYPE_CHECKING

from .models import Deal
from .rules import build_trade_context, validate_all

if TYPE_CHECKING:
    from .rules.tick_context import TradeRuleTickContext


def validate_deal(
    deal: Deal,
    current_date: Optional[date] = None,
    allow_locked_by_deal_id: Optional[str] = None,
    db_path: Optional[str] = None,
    tick_ctx: Optional["TradeRuleTickContext"] = None,
    integrity_check: Optional[bool] = None,
) -> None:
    ctx = build_trade_context(deal, current_date=current_date, db_path=db_path, tick_ctx=tick_ctx)
    try:
        if integrity_check is None:
            integrity_check = tick_ctx is None
        if integrity_check:
            ctx.repo.validate_integrity()
        validate_all(deal, ctx)
    finally:
        # Validator closes ctx.repo only if it owns the repo.
        if getattr(ctx, "owns_repo", True):
            repo = getattr(ctx, "repo", None)
            if repo is not None:
                repo.close()
