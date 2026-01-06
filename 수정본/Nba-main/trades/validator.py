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
    from contracts.store import get_league_season_year
    from contracts.sync import (
        sync_roster_salaries_for_season,
        sync_roster_teams_from_state,
    )

    season_year = get_league_season_year(GAME_STATE)
    sync_roster_teams_from_state(GAME_STATE)
    sync_roster_salaries_for_season(GAME_STATE, season_year)

    # RULES ENGINE CHECKS (migrated): deadline
    ctx = build_trade_context(current_date=current_date)
    validate_all(deal, ctx)

