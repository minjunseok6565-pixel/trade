from __future__ import annotations

from dataclasses import dataclass

from ...errors import DEAL_INVALIDATED, MISSING_TO_TEAM, TradeError
from ...models import PlayerAsset
from ..base import TradeContext


@dataclass
class ReturnToTradingTeamRule:
    rule_id: str = "return_to_trading_team_same_season"
    # Run after player eligibility but before salary matching.
    priority: int = 72
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        season_year = int(ctx.game_state.get("league", {}).get("season_year") or 0)
        if season_year <= 0:
            return
        season_key = str(season_year)

        for from_team, assets in deal.legs.items():
            for asset in assets:
                if not isinstance(asset, PlayerAsset):
                    continue
                to_team = _resolve_receiver(deal, from_team, asset)
                player_state = ctx.game_state.get("players", {}).get(asset.player_id)
                if not player_state:
                    continue
                banned = player_state.get("trade_return_bans", {}).get(season_key, [])
                if to_team in banned:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Player cannot return to trading team in same season",
                        {
                            "rule": self.rule_id,
                            "player_id": asset.player_id,
                            "from_team": from_team,
                            "to_team": to_team,
                            "season_year": season_year,
                            "reason": "same_season_return_to_trading_team",
                        },
                    )


def _resolve_receiver(deal, team_id: str, asset: PlayerAsset) -> str:
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
