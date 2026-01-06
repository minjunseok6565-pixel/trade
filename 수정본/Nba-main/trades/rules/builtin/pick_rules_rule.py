from __future__ import annotations

from dataclasses import dataclass

from ...errors import DEAL_INVALIDATED, MISSING_TO_TEAM, TradeError
from ...models import PickAsset
from ..base import TradeContext


@dataclass
class PickRulesRule:
    rule_id: str = "pick_rules"
    priority: int = 80
    enabled: bool = False

    def validate(self, deal, ctx: TradeContext) -> None:
        trade_rules = ctx.game_state.get("league", {}).get("trade_rules", {})
        max_pick_years_ahead = int(trade_rules.get("max_pick_years_ahead") or 7)
        stepien_lookahead = int(trade_rules.get("stepien_lookahead") or 7)

        league = ctx.game_state.get("league", {})
        try:
            current_season_year = int(league.get("draft_year") or 0)
        except (TypeError, ValueError):
            current_season_year = 0
        if current_season_year <= 0:
            raise TradeError(
                DEAL_INVALIDATED,
                "Missing league draft_year",
                {
                    "rule": self.rule_id,
                    "reason": "missing_draft_year",
                },
            )

        draft_picks = ctx.game_state.get("draft_picks", {})

        for assets in deal.legs.values():
            for asset in assets:
                if not isinstance(asset, PickAsset):
                    continue
                pick = draft_picks.get(asset.pick_id)
                if not pick:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Pick not found",
                        {
                            "rule": self.rule_id,
                            "pick_id": asset.pick_id,
                            "reason": "missing_pick",
                        },
                    )
                pick_year = int(pick.get("year") or 0)
                if pick_year > current_season_year + max_pick_years_ahead:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Pick too far in future",
                        {
                            "rule": self.rule_id,
                            "pick_id": asset.pick_id,
                            "reason": "pick_too_far",
                            "year": pick_year,
                            "current_season_year": current_season_year,
                            "max_pick_years_ahead": max_pick_years_ahead,
                        },
                    )

        owner_after = {
            pick_id: str(pick.get("owner_team") or "")
            for pick_id, pick in draft_picks.items()
        }
        for team_id, assets in deal.legs.items():
            for asset in assets:
                if not isinstance(asset, PickAsset):
                    continue
                receiver = _resolve_receiver(deal, team_id, asset)
                owner_after[asset.pick_id] = receiver

        if stepien_lookahead <= 0:
            return

        for team_id in deal.teams:
            for year in range(
                current_season_year + 1,
                current_season_year + stepien_lookahead,
            ):
                count_year = _count_first_round_picks_for_year(
                    draft_picks, owner_after, team_id, year
                )
                count_next = _count_first_round_picks_for_year(
                    draft_picks, owner_after, team_id, year + 1
                )
                if count_year == 0 and count_next == 0:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Stepien rule violation",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "reason": "stepien_violation",
                            "trade_date": ctx.current_date.isoformat(),
                            "year": year,
                            "lookahead": stepien_lookahead,
                        },
                    )


def _resolve_receiver(deal, team_id: str, asset: PickAsset) -> str:
    if asset.to_team:
        return asset.to_team
    if len(deal.teams) == 2:
        other_team = [team for team in deal.teams if team != team_id]
        if other_team:
            return other_team[0]
    raise TradeError(
        MISSING_TO_TEAM,
        "Missing to_team for multi-team deal asset",
        {"team_id": team_id, "asset": asset},
    )


def _count_first_round_picks_for_year(
    draft_picks: dict,
    owner_after: dict[str, str],
    team_id: str,
    year: int,
) -> int:
    count = 0
    for pick_id, pick in draft_picks.items():
        if int(pick.get("year") or 0) != year:
            continue
        if int(pick.get("round") or 0) != 1:
            continue
        if owner_after.get(pick_id) == team_id:
            count += 1
    return count
