from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional, Protocol


@dataclass
class TradeContext:
    game_state: dict
    roster_df: Any
    current_date: date
    extra: dict[str, Any] = field(default_factory=dict)


class Rule(Protocol):
    rule_id: str
    priority: int
    enabled: bool

    def validate(self, deal: Any, ctx: TradeContext) -> None:
        ...


def build_player_moves(deal: Any) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    from ..models import PlayerAsset

    players_out: dict[str, list[int]] = {team_id: [] for team_id in deal.teams}
    players_in: dict[str, list[int]] = {team_id: [] for team_id in deal.teams}

    for team_id, assets in deal.legs.items():
        for asset in assets:
            if not isinstance(asset, PlayerAsset):
                continue
            players_out[team_id].append(asset.player_id)
            receiver = _resolve_receiver(deal, team_id, asset)
            players_in[receiver].append(asset.player_id)

    return players_out, players_in


def _sum_player_salaries(roster_df: Any, player_ids: list[int]) -> float:
    if not player_ids:
        return 0.0
    return float(roster_df.reindex(player_ids)["SalaryAmount"].fillna(0.0).sum())


def build_team_trade_totals(
    deal: Any,
    ctx: TradeContext,
) -> dict[str, dict[str, float | int]]:
    players_out, players_in = build_player_moves(deal)
    totals: dict[str, dict[str, float | int]] = {}

    for team_id in deal.teams:
        outgoing_players = players_out.get(team_id, [])
        incoming_players = players_in.get(team_id, [])
        totals[team_id] = {
            "outgoing_salary": _sum_player_salaries(ctx.roster_df, outgoing_players),
            "incoming_salary": _sum_player_salaries(ctx.roster_df, incoming_players),
            "outgoing_players_count": len(outgoing_players),
            "incoming_players_count": len(incoming_players),
        }

    return totals


def build_team_payrolls(
    deal: Any,
    ctx: TradeContext,
    trade_totals: Optional[dict[str, dict[str, float | int]]] = None,
) -> dict[str, dict[str, float]]:
    totals = trade_totals or build_team_trade_totals(deal, ctx)
    payrolls: dict[str, dict[str, float]] = {}

    for team_id in deal.teams:
        payroll_before = float(
            ctx.roster_df.loc[ctx.roster_df["Team"] == team_id, "SalaryAmount"].sum()
        )
        outgoing_salary = float(totals[team_id]["outgoing_salary"])
        incoming_salary = float(totals[team_id]["incoming_salary"])
        payrolls[team_id] = {
            "payroll_before": payroll_before,
            "payroll_after": payroll_before - outgoing_salary + incoming_salary,
        }

    return payrolls


def _resolve_receiver(deal: Any, sender_team: str, asset: Any) -> str:
    if getattr(asset, "to_team", None):
        return asset.to_team
    if len(deal.teams) == 2:
        other_team = [team for team in deal.teams if team != sender_team]
        if other_team:
            return other_team[0]
    from ..errors import MISSING_TO_TEAM, TradeError

    raise TradeError(
        MISSING_TO_TEAM,
        "Missing to_team for multi-team deal asset",
        {"team_id": sender_team, "asset": asset},
    )


def build_trade_context(
    current_date: Optional[date] = None,
    extra: Optional[dict[str, Any]] = None,
) -> TradeContext:
    from config import ROSTER_DF
    import state as state_module

    if current_date is None:
        get_current_date_as_date = getattr(state_module, "get_current_date_as_date", None)
        if callable(get_current_date_as_date):
            current_date = get_current_date_as_date()
        else:
            get_current_date = getattr(state_module, "get_current_date", None)
            current = get_current_date() if callable(get_current_date) else None
            if current:
                try:
                    current_date = date.fromisoformat(str(current))
                except ValueError:
                    current_date = date.today()
            else:
                current_date = date.today()

    resolved_extra = dict(extra) if extra else {}
    if "allow_locked_by_deal_id" not in resolved_extra:
        import inspect

        frame = inspect.currentframe()
        caller = frame.f_back if frame else None
        allow_locked_by_deal_id = caller.f_locals.get("allow_locked_by_deal_id") if caller else None
        if allow_locked_by_deal_id is not None:
            resolved_extra["allow_locked_by_deal_id"] = allow_locked_by_deal_id

    return TradeContext(
        game_state=state_module.GAME_STATE,
        roster_df=ROSTER_DF,
        current_date=current_date,
        extra=resolved_extra,
    )
