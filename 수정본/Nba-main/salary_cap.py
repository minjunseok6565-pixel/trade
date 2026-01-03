from __future__ import annotations

from typing import List

from config import HARD_CAP as CONFIG_HARD_CAP, ROSTER_DF
from state import GAME_STATE


def _get_hard_cap() -> float:
    try:
        return float(CONFIG_HARD_CAP)
    except Exception:
        pass
    league = GAME_STATE.get("league") or {}
    trade_rules = league.get("trade_rules") or {}
    return float(trade_rules.get("hard_cap") or 0.0)


def compute_team_payroll(team_id: str) -> float:
    team_id = team_id.upper()
    try:
        payroll = float(ROSTER_DF.loc[ROSTER_DF["Team"] == team_id, "SalaryAmount"].sum())
    except Exception:
        payroll = 0.0
    return payroll


def compute_payroll_after_player_moves(
    team_id: str,
    players_out: List[int],
    players_in: List[int],
) -> float:
    team_id = team_id.upper()
    payroll = compute_team_payroll(team_id)
    players_out = players_out or []
    players_in = players_in or []

    if players_out:
        payroll -= float(ROSTER_DF.reindex(players_out)["SalaryAmount"].fillna(0.0).sum())
    if players_in:
        payroll += float(ROSTER_DF.reindex(players_in)["SalaryAmount"].fillna(0.0).sum())
    return payroll


def compute_cap_space(team_id: str) -> float:
    hard_cap = _get_hard_cap()
    return hard_cap - compute_team_payroll(team_id)


HARD_CAP = _get_hard_cap()
