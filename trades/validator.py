from __future__ import annotations

from datetime import date
from typing import Optional

from state import ensure_league_block, export_workflow_state
from .models import Deal
from .rules import build_trade_context, validate_all


def validate_deal(
    deal: Deal,
    current_date: Optional[date] = None,
    allow_locked_by_deal_id: Optional[str] = None,
) -> None:
    ensure_league_block()

    db_path = (export_workflow_state().get("league") or {}).get("db_path")
    ctx = build_trade_context(current_date=current_date, db_path=db_path)
    try:
        ctx.repo.validate_integrity()
        validate_all(deal, ctx)
    finally:
        # Validator closes ctx.repo to avoid SQLite connection leaks.
        repo = getattr(ctx, "repo", None)
        if repo is not None:
            repo.close()
