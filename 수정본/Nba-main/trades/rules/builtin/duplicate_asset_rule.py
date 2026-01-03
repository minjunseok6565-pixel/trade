from __future__ import annotations

from dataclasses import dataclass

from ...errors import DUPLICATE_ASSET, TradeError
from ...models import PickAsset, PlayerAsset
from ..base import TradeContext


@dataclass
class DuplicateAssetRule:
    rule_id: str = "duplicate_asset"
    priority: int = 30
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        seen_assets: dict[str, str] = {}
        for team_id, assets in deal.legs.items():
            for asset in assets:
                if isinstance(asset, PlayerAsset):
                    asset_key = f"player:{asset.player_id}"
                elif isinstance(asset, PickAsset):
                    asset_key = f"pick:{asset.pick_id}"
                else:
                    continue
                if asset_key in seen_assets:
                    raise TradeError(
                        DUPLICATE_ASSET,
                        "Duplicate asset in deal",
                        {
                            "asset_key": asset_key,
                            "first_sender": seen_assets[asset_key],
                            "duplicate_sender": team_id,
                        },
                    )
                seen_assets[asset_key] = team_id
