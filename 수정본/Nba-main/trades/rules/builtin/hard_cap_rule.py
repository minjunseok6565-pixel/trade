from __future__ import annotations

from dataclasses import dataclass

from config import HARD_CAP as CONFIG_HARD_CAP
from ...errors import HARD_CAP_EXCEEDED, TradeError
from ..base import TradeContext, build_team_payrolls, build_team_trade_totals


@dataclass
class HardCapRule:
    rule_id: str = "hard_cap"
    priority: int = 70
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        league_rules = ctx.game_state.get("league", {}).get("trade_rules", {})
        hard_cap = league_rules.get("hard_cap", CONFIG_HARD_CAP)

        trade_totals = build_team_trade_totals(deal, ctx)
        payrolls = build_team_payrolls(deal, ctx, trade_totals)
        for team_id in deal.teams:
            payroll_after = payrolls[team_id]["payroll_after"]
            if payroll_after > hard_cap:
                raise TradeError(
                    HARD_CAP_EXCEEDED,
                    "Hard cap exceeded",
                    {"team_id": team_id, "payroll": payroll_after, "hard_cap": hard_cap},
                )
