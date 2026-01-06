from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ...errors import DEAL_INVALIDATED, TradeError
from ...models import PlayerAsset
from ..base import TradeContext


@dataclass
class PlayerEligibilityRule:
    rule_id: str = "player_eligibility"
    priority: int = 70
    enabled: bool = False

    def validate(self, deal, ctx: TradeContext) -> None:
        trade_rules = ctx.game_state.get("league", {}).get("trade_rules", {})
        new_fa_sign_ban_days = int(trade_rules.get("new_fa_sign_ban_days") or 90)
        aggregation_ban_days = int(trade_rules.get("aggregation_ban_days") or 60)

        for team_id, assets in deal.legs.items():
            for asset in assets:
                if not isinstance(asset, PlayerAsset):
                    continue
                player_state = ctx.game_state.get("players", {}).get(asset.player_id, {})
                if not player_state:
                    continue
                if not player_state.get("signed_via_free_agency"):
                    continue
                signed_date = _parse_player_date(player_state.get("signed_date"))
                banned_until = signed_date + timedelta(days=new_fa_sign_ban_days)
                if ctx.current_date < banned_until:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Player recently signed via free agency",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "player_id": asset.player_id,
                            "reason": "recent_free_agent_signing",
                            "trade_date": ctx.current_date.isoformat(),
                            "signed_date": signed_date.isoformat(),
                        },
                    )

        for team_id in deal.teams:
            outgoing_assets = deal.legs.get(team_id, [])
            outgoing_players = [
                asset for asset in outgoing_assets if isinstance(asset, PlayerAsset)
            ]
            if len(outgoing_players) < 2:
                continue
            for asset in outgoing_players:
                player_state = ctx.game_state.get("players", {}).get(asset.player_id, {})
                if not player_state:
                    continue
                if not player_state.get("acquired_via_trade"):
                    continue
                acquired_date = _parse_player_date(player_state.get("acquired_date"))
                banned_until = acquired_date + timedelta(days=aggregation_ban_days)
                if ctx.current_date < banned_until:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Recently traded players cannot be aggregated",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "player_id": asset.player_id,
                            "reason": "aggregation_ban",
                            "trade_date": ctx.current_date.isoformat(),
                            "acquired_date": acquired_date.isoformat(),
                        },
                    )


def _parse_player_date(value: object) -> date:
    """Parse player date strings like '2026-01-05T10:11:12' as 2026-01-05."""
    if value:
        s = str(value).strip()
        if len(s) >= 10:
            try:
                # Slice first 10 chars to accept ISO datetimes with time components.
                return date.fromisoformat(s[:10])
            except ValueError:
                pass
    return date(1900, 1, 1)
