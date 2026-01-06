from __future__ import annotations

from dataclasses import dataclass
import math

from ...errors import DEAL_INVALIDATED, TradeError
from ..base import TradeContext, build_team_trade_totals, build_team_payrolls


@dataclass
class SalaryMatchingRule:
    rule_id: str = "salary_matching"
    priority: int = 90
    enabled: bool = False

    def validate(self, deal, ctx: TradeContext) -> None:
        trade_rules = ctx.game_state.get("league", {}).get("trade_rules", {})
        salary_cap = float(trade_rules.get("salary_cap") or 0.0)
        first_apron = float(trade_rules.get("first_apron") or 0.0)
        second_apron = float(trade_rules.get("second_apron") or 0.0)
        match_small_out_max = float(trade_rules.get("match_small_out_max") or 7_500_000)
        match_mid_out_max = float(trade_rules.get("match_mid_out_max") or 29_000_000)
        match_mid_add = float(trade_rules.get("match_mid_add") or 7_500_000)
        match_buffer = float(trade_rules.get("match_buffer") or 250_000)
        first_apron_mult = float(trade_rules.get("first_apron_mult") or 1.10)
        second_apron_mult = float(trade_rules.get("second_apron_mult") or 1.00)

        trade_totals = build_team_trade_totals(deal, ctx)
        payrolls = build_team_payrolls(deal, ctx, trade_totals=trade_totals)

        for team_id in deal.teams:
            totals = trade_totals[team_id]
            outgoing_salary = float(totals.get("outgoing_salary") or 0.0)
            incoming_salary = float(totals.get("incoming_salary") or 0.0)
            outgoing_players = int(totals.get("outgoing_players_count") or 0)
            incoming_players = int(totals.get("incoming_players_count") or 0)

            if incoming_salary == 0:
                continue

            payroll_before = float(payrolls[team_id].get("payroll_before") or 0.0)
            payroll_after = float(payrolls[team_id].get("payroll_after") or 0.0)

            status = _resolve_apron_status(payroll_after, first_apron, second_apron)

            if payroll_before < salary_cap:
                cap_room = salary_cap - payroll_before
                max_incoming = cap_room + outgoing_salary
                if incoming_salary <= max_incoming:
                    continue

            if outgoing_salary <= 0:
                raise TradeError(
                    DEAL_INVALIDATED,
                    "Salary matching failed",
                    {
                        "rule": self.rule_id,
                        "team_id": team_id,
                        "status": status,
                        "payroll_before": payroll_before,
                        "payroll_after": payroll_after,
                        "outgoing_salary": outgoing_salary,
                        "incoming_salary": incoming_salary,
                        "allowed_in": 0.0,
                        "method": "outgoing_required",
                    },
                )

            if status == "SECOND_APRON":
                if outgoing_players > 1 or incoming_players > 1:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Salary matching failed",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "status": status,
                            "payroll_before": payroll_before,
                            "payroll_after": payroll_after,
                            "outgoing_salary": outgoing_salary,
                            "incoming_salary": incoming_salary,
                            "allowed_in": 0.0,
                            "method": "second_apron_one_for_one",
                        },
                    )
                allowed_in = math.floor(outgoing_salary * second_apron_mult)
                method = "outgoing_second_apron"
            elif status == "FIRST_APRON":
                allowed_in = math.floor(outgoing_salary * first_apron_mult)
                method = "outgoing_first_apron"
            else:
                if outgoing_salary <= match_small_out_max:
                    allowed_in = 2 * outgoing_salary + match_buffer
                elif outgoing_salary <= match_mid_out_max:
                    allowed_in = outgoing_salary + match_mid_add
                else:
                    allowed_in = math.floor(outgoing_salary * 1.25) + match_buffer
                method = "outgoing_below_first_apron"

            if incoming_salary > allowed_in:
                raise TradeError(
                    DEAL_INVALIDATED,
                    "Salary matching failed",
                    {
                        "rule": self.rule_id,
                        "team_id": team_id,
                        "status": status,
                        "payroll_before": payroll_before,
                        "payroll_after": payroll_after,
                        "outgoing_salary": outgoing_salary,
                        "incoming_salary": incoming_salary,
                        "allowed_in": allowed_in,
                        "method": method,
                    },
                )


def _resolve_apron_status(
    payroll_after: float, first_apron: float, second_apron: float
) -> str:
    if payroll_after >= second_apron:
        return "SECOND_APRON"
    if payroll_after >= first_apron:
        return "FIRST_APRON"
    return "BELOW_FIRST_APRON"
