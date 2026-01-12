from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...errors import ASSET_LOCKED, TradeError
from ...models import FixedAsset, PickAsset, PlayerAsset, SwapAsset, asset_key
from ..base import TradeContext


@dataclass
class AssetLockRule:
    rule_id: str = "asset_lock"
    priority: int = 40
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        allow_locked_by_deal_id = ctx.extra.get("allow_locked_by_deal_id")

        for team_id, assets in deal.legs.items():
            for asset in assets:
                asset_key_value = asset_key(asset)

                lock = ctx.repo.get_asset_lock(asset_key_value)
                if not lock:
                    continue

                locked_deal_id = lock["deal_id"]
                expires_at = lock["expires_at"]
                expires_at_date = None
                if expires_at is not None:
                    if isinstance(expires_at, date):
                        expires_at_date = expires_at
                    else:
                        try:
                            expires_at_date = date.fromisoformat(str(expires_at))
                        except ValueError:
                            raise TradeError(
                                ASSET_LOCKED,
                                "Asset lock expiry could not be parsed",
                                {
                                    "asset_key": asset_key_value,
                                    "deal_id": locked_deal_id,
                                    "expires_at": expires_at,
                                },
                            )

                if expires_at_date is not None and ctx.current_date > expires_at_date:
                    ctx.repo.release_asset_lock(asset_key_value)
                    continue

                if allow_locked_by_deal_id and locked_deal_id == allow_locked_by_deal_id:
                    continue

                raise TradeError(
                    ASSET_LOCKED,
                    "Asset is locked",
                    {
                        "asset_key": asset_key_value,
                        "deal_id": locked_deal_id,
                        "expires_at": expires_at,
                    },
                )
