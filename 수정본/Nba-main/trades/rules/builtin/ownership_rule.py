from __future__ import annotations

from dataclasses import dataclass

from ...errors import PICK_NOT_OWNED, PLAYER_NOT_OWNED, TradeError
from ...models import PickAsset, PlayerAsset
from ..base import TradeContext


@dataclass
class OwnershipRule:
    rule_id: str = "ownership"
    priority: int = 50
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        for team_id, assets in deal.legs.items():
            for asset in assets:
                if isinstance(asset, PlayerAsset):
                    try:
                        current_team = str(ctx.roster_df.at[asset.player_id, "Team"]).upper()
                    except Exception:
                        current_team = ""
                    if current_team != team_id:
                        raise TradeError(
                            PLAYER_NOT_OWNED,
                            "Player not owned by team",
                            {"player_id": asset.player_id, "team_id": team_id},
                        )
                if isinstance(asset, PickAsset):
                    draft_picks = ctx.game_state.get("draft_picks", {})
                    pick = draft_picks.get(asset.pick_id)
                    if not pick:
                        raise TradeError(
                            PICK_NOT_OWNED,
                            "Pick not found",
                            {"pick_id": asset.pick_id, "team_id": team_id},
                        )
                    if str(pick.get("owner_team", "")).upper() != team_id:
                        raise TradeError(
                            PICK_NOT_OWNED,
                            "Pick not owned by team",
                            {"pick_id": asset.pick_id, "team_id": team_id},
                        )
