from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from schema import normalize_player_id, normalize_team_id

from ...errors import (
    FIXED_ASSET_NOT_FOUND,
    FIXED_ASSET_NOT_OWNED,
    PICK_NOT_OWNED,
    PLAYER_NOT_OWNED,
    PROTECTION_CONFLICT,
    SWAP_INVALID,
    SWAP_NOT_OWNED,
    TradeError,
)
from ...models import FixedAsset, PickAsset, PlayerAsset, SwapAsset
from ..base import TradeContext


@dataclass
class OwnershipRule:
    rule_id: str = "ownership"
    priority: int = 50
    enabled: bool = True

    def _get_assets_snapshot(self, ctx: TradeContext) -> Dict[str, Dict[str, Any]]:
        """
        Read draft_picks / swap_rights / fixed_assets from DB (SSOT) in a way that:
          - prefers a precomputed snapshot on ctx.extra["assets_snapshot"] if present
          - otherwise pulls a consistent snapshot via repo.get_trade_assets_snapshot()
          - falls back to individual map getters if needed
        Always returns a dict with keys: draft_picks, swap_rights, fixed_assets.
        """
        # 1) Prefer precomputed snapshot injected by build_trade_context (if the caller added it)
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict):
            snap = extra.get("assets_snapshot")
            if isinstance(snap, dict):
                draft_picks = snap.get("draft_picks") if isinstance(snap.get("draft_picks"), dict) else {}
                swap_rights = snap.get("swap_rights") if isinstance(snap.get("swap_rights"), dict) else {}
                fixed_assets = snap.get("fixed_assets") if isinstance(snap.get("fixed_assets"), dict) else {}
                return {"draft_picks": draft_picks, "swap_rights": swap_rights, "fixed_assets": fixed_assets}

        repo = getattr(ctx, "repo", None)
        if repo is None:
            # TradeContext without repo should not happen in production, but keep rule deterministic.
            return {"draft_picks": {}, "swap_rights": {}, "fixed_assets": {}}

        # 2) Best path: consistent snapshot in one DB transaction
        if hasattr(repo, "get_trade_assets_snapshot"):
            snap = repo.get_trade_assets_snapshot()
            if isinstance(snap, dict):
                return {
                    "draft_picks": snap.get("draft_picks") if isinstance(snap.get("draft_picks"), dict) else {},
                    "swap_rights": snap.get("swap_rights") if isinstance(snap.get("swap_rights"), dict) else {},
                    "fixed_assets": snap.get("fixed_assets") if isinstance(snap.get("fixed_assets"), dict) else {},
                }

        # 3) Fallback: individual map reads (still DB-backed, but not guaranteed consistent across reads)
        draft_picks = repo.get_draft_picks_map() if hasattr(repo, "get_draft_picks_map") else {}
        swap_rights = repo.get_swap_rights_map() if hasattr(repo, "get_swap_rights_map") else {}
        fixed_assets = repo.get_fixed_assets_map() if hasattr(repo, "get_fixed_assets_map") else {}
        return {"draft_picks": draft_picks, "swap_rights": swap_rights, "fixed_assets": fixed_assets}

    def validate(self, deal, ctx: TradeContext) -> None:
        assets_snapshot = self._get_assets_snapshot(ctx)
        draft_picks: Dict[str, Dict[str, Any]] = assets_snapshot.get("draft_picks", {})  # type: ignore[assignment]
        swap_rights: Dict[str, Dict[str, Any]] = assets_snapshot.get("swap_rights", {})  # type: ignore[assignment]
        fixed_assets: Dict[str, Dict[str, Any]] = assets_snapshot.get("fixed_assets", {})  # type: ignore[assignment]

        for team_id, assets in deal.legs.items():
            team_id_normalized = str(normalize_team_id(team_id, strict=True)).upper()
            for asset in assets:
                if isinstance(asset, PlayerAsset):
                    try:
                        pid = str(normalize_player_id(asset.player_id, strict=False, allow_legacy_numeric=True))
                        current_team = ctx.repo.get_team_id_by_player(pid)
                    except Exception as exc:
                        raise ValueError(
                            f"Player not found in roster: {asset.player_id}"
                        ) from exc
                    if str(current_team).upper() != team_id_normalized:
                        raise TradeError(
                            PLAYER_NOT_OWNED,
                            "Player not owned by team",
                            {"player_id": asset.player_id, "team_id": team_id},
                        )
                if isinstance(asset, PickAsset):
                    pick = draft_picks.get(asset.pick_id)
                    if not pick:
                        raise TradeError(
                            PICK_NOT_OWNED,
                            "Pick not found",
                            {"pick_id": asset.pick_id, "team_id": team_id},
                        )
                    # Ensure teams cannot trade picks they do not own.
                    current_owner = str(pick.get("owner_team", "")).upper()
                    if current_owner != team_id_normalized:
                        raise TradeError(
                            PICK_NOT_OWNED,
                            "Pick not owned by team",
                            {
                                "pick_id": asset.pick_id,
                                "team_id": team_id,
                                "owner_team": current_owner,
                            },
                        )
                    if asset.protection is not None:
                        existing_protection = pick.get("protection")
                        if existing_protection is not None and existing_protection != asset.protection:
                            raise TradeError(
                                PROTECTION_CONFLICT,
                                "Pick protection conflicts with existing record",
                                {
                                    "pick_id": asset.pick_id,
                                    "existing_protection": existing_protection,
                                    "attempted_protection": asset.protection,
                                },
                            )
                if isinstance(asset, FixedAsset):
                    fixed = fixed_assets.get(asset.asset_id)
                    if not fixed:
                        raise TradeError(
                            FIXED_ASSET_NOT_FOUND,
                            "Fixed asset not found",
                            {"asset_id": asset.asset_id, "team_id": team_id},
                        )
                    if str(fixed.get("owner_team", "")).upper() != team_id_normalized:
                        raise TradeError(
                            FIXED_ASSET_NOT_OWNED,
                            "Fixed asset not owned by team",
                            {"asset_id": asset.asset_id, "team_id": team_id},
                        )
                if isinstance(asset, SwapAsset):
                    pick_a = draft_picks.get(asset.pick_id_a)
                    pick_b = draft_picks.get(asset.pick_id_b)
                    if not pick_a or not pick_b:
                        raise TradeError(
                            SWAP_INVALID,
                            "Swap picks must exist",
                            {
                                "swap_id": asset.swap_id,
                                "pick_id_a": asset.pick_id_a,
                                "pick_id_b": asset.pick_id_b,
                            },
                        )
                    if pick_a.get("year") != pick_b.get("year") or pick_a.get("round") != pick_b.get("round"):
                        raise TradeError(
                            SWAP_INVALID,
                            "Swap picks must match year and round",
                            {
                                "swap_id": asset.swap_id,
                                "pick_a": {"year": pick_a.get("year"), "round": pick_a.get("round")},
                                "pick_b": {"year": pick_b.get("year"), "round": pick_b.get("round")},
                            },
                        )
                    swap = swap_rights.get(asset.swap_id)
                    if swap:
                        if str(swap.get("owner_team", "")).upper() != team_id_normalized:
                            raise TradeError(
                                SWAP_NOT_OWNED,
                                "Swap right not owned by team",
                                {"swap_id": asset.swap_id, "team_id": team_id},
                            )
                    else:
                        owner_a = str(pick_a.get("owner_team", "")).upper()
                        owner_b = str(pick_b.get("owner_team", "")).upper()
                        if owner_a != team_id_normalized and owner_b != team_id_normalized:
                            raise TradeError(
                                SWAP_INVALID,
                                "Swap right cannot be created by team",
                                {
                                    "swap_id": asset.swap_id,
                                    "team_id": team_id,
                                    "pick_id_a": asset.pick_id_a,
                                    "pick_id_b": asset.pick_id_b,
                                    "pick_owner_a": owner_a,
                                    "pick_owner_b": owner_b,
                                },
                            )
