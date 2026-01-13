from __future__ import annotations

from dataclasses import dataclass

from ...errors import DUPLICATE_ASSET, TradeError
from ...models import FixedAsset, PickAsset, PlayerAsset, SwapAsset, asset_key
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
                key = asset_key(asset)
                if key in seen_assets:
                    raise TradeError(
                        DUPLICATE_ASSET,
                        "Duplicate asset in deal",
                        {
                            "asset_key": key,
                            "first_sender": seen_assets[key],
                            "duplicate_sender": team_id,
                        },
                    )
                seen_assets[key] = team_id
