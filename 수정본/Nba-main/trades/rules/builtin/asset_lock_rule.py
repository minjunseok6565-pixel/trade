from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...errors import ASSET_LOCKED, TradeError
from ...models import PickAsset, PlayerAsset
from ..base import TradeContext


@dataclass
class AssetLockRule:
    rule_id: str = "asset_lock"
    priority: int = 40
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        asset_locks = ctx.game_state.get("asset_locks", {})
        allow_locked_by_deal_id = ctx.extra.get("allow_locked_by_deal_id")

        for team_id, assets in deal.legs.items():
            for asset in assets:
                if isinstance(asset, PlayerAsset):
                    asset_key = f"player:{asset.player_id}"
                elif isinstance(asset, PickAsset):
                    asset_key = f"pick:{asset.pick_id}"
                else:
                    continue

                lock = asset_locks.get(asset_key)
                if not lock:
                    continue

                locked_deal_id = lock.get("deal_id")
                expires_at = lock.get("expires_at")
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
                                    "asset_key": asset_key,
                                    "deal_id": locked_deal_id,
                                    "expires_at": expires_at,
                                },
                            )

                if expires_at_date is not None and ctx.current_date > expires_at_date:
                    asset_locks.pop(asset_key, None)
                    continue

                if allow_locked_by_deal_id and locked_deal_id == allow_locked_by_deal_id:
                    continue

                raise TradeError(
                    ASSET_LOCKED,
                    "Asset is locked",
                    {
                        "asset_key": asset_key,
                        "deal_id": locked_deal_id,
                        "expires_at": expires_at,
                    },
                )
