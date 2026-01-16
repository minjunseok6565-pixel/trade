from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping

from ...errors import SWAP_INVALID, TradeError
from ...models import SwapAsset, compute_swap_id
from ..base import TradeContext


@dataclass
class SwapUniquenessRule:
    rule_id: str = "swap_uniqueness"
    priority: int = 35
    enabled: bool = True

    def _get_swap_rights_map(self, ctx: TradeContext) -> Dict[str, Dict[str, Any]]:
        """
        Read swap_rights from DB (SSOT), with a robust fallback strategy:
          1) ctx.extra["assets_snapshot"]["swap_rights"] if provided
          2) ctx.repo.get_trade_assets_snapshot()["swap_rights"] (consistent snapshot)
          3) ctx.repo.get_swap_rights_map()
          4) (legacy fallback) ctx.game_state.get("swap_rights", {})
        """
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict):
            snap = extra.get("assets_snapshot")
            if isinstance(snap, dict):
                sr = snap.get("swap_rights")
                if isinstance(sr, dict):
                    return sr  # type: ignore[return-value]

        repo = getattr(ctx, "repo", None)
        if repo is not None:
            if hasattr(repo, "get_trade_assets_snapshot"):
                snap = repo.get_trade_assets_snapshot()
                if isinstance(snap, dict):
                    sr = snap.get("swap_rights")
                    if isinstance(sr, dict):
                        return sr  # type: ignore[return-value]
            if hasattr(repo, "get_swap_rights_map"):
                sr = repo.get_swap_rights_map()
                if isinstance(sr, dict):
                    return sr  # type: ignore[return-value]

        # Legacy fallback (should become unused once state keys are removed)
        gs = getattr(ctx, "game_state", None)
        if isinstance(gs, dict):
            sr = gs.get("swap_rights", {})
            if isinstance(sr, dict):
                return sr  # type: ignore[return-value]
        return {}

    @staticmethod
    def _is_active(record: Mapping[str, Any]) -> bool:
        """
        Interpret active flag robustly across legacy/state and DB (0/1) representations.
        Default is active=True if missing.
        """
        v = record.get("active", True)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() not in ("0", "false", "no", "off", "")
        return True


    def validate(self, deal, ctx: TradeContext) -> None:
        swap_rights = self._get_swap_rights_map(ctx)
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
                for record in swap_rights.values():
                    if not isinstance(record, dict):
                        continue
                    if not self._is_active(record):
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
