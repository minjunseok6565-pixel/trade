from __future__ import annotations

from dataclasses import dataclass

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

    def validate(self, deal, ctx: TradeContext) -> None:
        for team_id, assets in deal.legs.items():
            for asset in assets:
                if isinstance(asset, PlayerAsset):
                    try:
                        pid = str(normalize_player_id(asset.player_id, strict=False, allow_legacy_numeric=True))
                        current_team = ctx.repo.get_team_id_by_player(pid)
                    except Exception as exc:
                        raise ValueError(
                            f"Player not found in roster: {asset.player_id}"
                        ) from exc
                    team_id_normalized = str(normalize_team_id(team_id, strict=True))
                    if current_team != team_id_normalized:
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
                    # Ensure teams cannot trade picks they do not own.
                    current_owner = str(pick.get("owner_team", "")).upper()
                    if current_owner != team_id:
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
                    fixed_assets = ctx.game_state.get("fixed_assets", {})
                    fixed = fixed_assets.get(asset.asset_id)
                    if not fixed:
                        raise TradeError(
                            FIXED_ASSET_NOT_FOUND,
                            "Fixed asset not found",
                            {"asset_id": asset.asset_id, "team_id": team_id},
                        )
                    if str(fixed.get("owner_team", "")).upper() != team_id:
                        raise TradeError(
                            FIXED_ASSET_NOT_OWNED,
                            "Fixed asset not owned by team",
                            {"asset_id": asset.asset_id, "team_id": team_id},
                        )
                if isinstance(asset, SwapAsset):
                    draft_picks = ctx.game_state.get("draft_picks", {})
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
                    swap_rights = ctx.game_state.get("swap_rights", {})
                    swap = swap_rights.get(asset.swap_id)
                    if swap:
                        if str(swap.get("owner_team", "")).upper() != team_id:
                            raise TradeError(
                                SWAP_NOT_OWNED,
                                "Swap right not owned by team",
                                {"swap_id": asset.swap_id, "team_id": team_id},
                            )
                    else:
                        owner_a = str(pick_a.get("owner_team", "")).upper()
                        owner_b = str(pick_b.get("owner_team", "")).upper()
                        if owner_a != team_id and owner_b != team_id:
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
