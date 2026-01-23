from __future__ import annotations

from datetime import date
from typing import Optional

from state import get_db_path
from .models import Deal
from .rules import build_trade_context, validate_all


def validate_deal(
    deal: Deal,
    current_date: Optional[date] = None,
    allow_locked_by_deal_id: Optional[str] = None,
) -> None:
    db_path = get_db_path()
    ctx = build_trade_context(current_date=current_date, db_path=db_path)
    try:
        ctx.repo.validate_integrity()
        validate_all(deal, ctx)
    finally:
        # Validator closes ctx.repo to avoid SQLite connection leaks.
        repo = getattr(ctx, "repo", None)
        if repo is not None:
            repo.close()
