from __future__ import annotations

from dataclasses import dataclass

from config import HARD_CAP as CONFIG_HARD_CAP
from salary_cap import compute_payroll_after_player_moves

from ...errors import HARD_CAP_EXCEEDED, TradeError
from ..base import TradeContext, build_player_moves


@dataclass
class HardCapRule:
    rule_id: str = "hard_cap"
    priority: int = 70
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        league_rules = ctx.game_state.get("league", {}).get("trade_rules", {})
        hard_cap = league_rules.get("hard_cap", CONFIG_HARD_CAP)

        players_out, players_in = build_player_moves(deal)
        for team_id in deal.teams:
            payroll_after = compute_payroll_after_player_moves(
                team_id,
                players_out[team_id],
                players_in[team_id],
            )
            if payroll_after > hard_cap:
                raise TradeError(
                    HARD_CAP_EXCEEDED,
                    "Hard cap exceeded",
                    {"team_id": team_id, "payroll": payroll_after, "hard_cap": hard_cap},
                )
