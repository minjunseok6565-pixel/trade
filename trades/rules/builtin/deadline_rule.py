from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...errors import TRADE_DEADLINE_PASSED, TradeError
from ..base import TradeContext


@dataclass
class DeadlineRule:
    rule_id: str = "deadline"
    priority: int = 10
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        trade_deadline = (
            ctx.game_state.get("league", {}).get("trade_rules", {}).get("trade_deadline")
        )
        if not trade_deadline:
            return

        try:
            deadline_date = date.fromisoformat(str(trade_deadline))
        except ValueError:
            return

        if ctx.current_date > deadline_date:
            raise TradeError(
                TRADE_DEADLINE_PASSED,
                "Trade deadline has passed",
                {
                    "current_date": ctx.current_date.isoformat(),
                    "deadline": str(trade_deadline),
                },
            )
