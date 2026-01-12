from __future__ import annotations

from dataclasses import dataclass

from ...errors import SWAP_INVALID, TradeError
from ...models import SwapAsset, compute_swap_id
from ..base import TradeContext


@dataclass
class SwapUniquenessRule:
    rule_id: str = "swap_uniqueness"
    priority: int = 35
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        for assets in deal.legs.values():
            for asset in assets:
                if not isinstance(asset, SwapAsset):
                    continue
                expected = compute_swap_id(asset.pick_id_a, asset.pick_id_b)
                if asset.swap_id != expected:
                    raise TradeError(
                        SWAP_INVALID,
                        "swap_id must be canonical for the pick pair",
                        {
                            "swap_id": asset.swap_id,
                            "expected": expected,
                            "pick_id_a": asset.pick_id_a,
                            "pick_id_b": asset.pick_id_b,
                        },
                    )
                pair_key = frozenset([asset.pick_id_a, asset.pick_id_b])
                for record in ctx.repo.list_swaps():
                    if not record.get("active", True):
                        continue
                    existing_pick_id_a = record.get("pick_id_a")
                    existing_pick_id_b = record.get("pick_id_b")
                    if not existing_pick_id_a or not existing_pick_id_b:
                        continue
                    existing_pair = frozenset([existing_pick_id_a, existing_pick_id_b])
                    if existing_pair == pair_key and record.get("swap_id") != asset.swap_id:
                        raise TradeError(
                            SWAP_INVALID,
                            "Active swap right already exists for this pick pair",
                            {
                                "swap_id": asset.swap_id,
                                "conflict_swap_id": record.get("swap_id"),
                                "pick_pair": sorted(list(pair_key)),
                            },
                        )
