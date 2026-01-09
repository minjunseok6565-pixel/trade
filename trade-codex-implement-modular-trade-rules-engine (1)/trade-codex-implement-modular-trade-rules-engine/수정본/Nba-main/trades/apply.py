from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any, Dict, List, Optional

from config import ROSTER_DF
from team_utils import _init_players_and_teams_if_needed
from state import GAME_STATE, get_current_date_as_date

from .errors import (
    APPLY_FAILED,
    FIXED_ASSET_NOT_FOUND,
    FIXED_ASSET_NOT_OWNED,
    PICK_NOT_OWNED,
    PROTECTION_CONFLICT,
    SWAP_INVALID,
    SWAP_NOT_OWNED,
    TradeError,
)
from .models import Deal, FixedAsset, PickAsset, PlayerAsset, SwapAsset
from .picks import transfer_pick
from .transaction_log import append_trade_transaction


def _collect_player_ids(deal: Deal) -> List[int]:
    player_ids: List[int] = []
    for assets in deal.legs.values():
        for asset in assets:
            if isinstance(asset, PlayerAsset):
                player_ids.append(asset.player_id)
    return player_ids


def _resolve_receiver(deal: Deal, sender_team: str, asset: Any) -> str:
    if asset.to_team:
        return asset.to_team
    if len(deal.teams) == 2:
        other_team = [team for team in deal.teams if team != sender_team]
        if other_team:
            return other_team[0]
    raise TradeError(APPLY_FAILED, "Missing to_team for multi-team deal asset")


def apply_deal(
    deal: Deal,
    source: str,
    deal_id: Optional[str] = None,
    trade_date: Optional[date] = None,
) -> Dict[str, Any]:
    _init_players_and_teams_if_needed()
    player_ids = _collect_player_ids(deal)
    original_teams: Dict[int, str] = {}
    original_trade_return_bans: Dict[int, Optional[Dict[str, List[str]]]] = {}
    original_pick_owners: Dict[str, str] = {}
    original_pick_protections: Dict[str, Any] = {}
    original_swap_rights: Dict[str, Optional[dict]] = {}
    original_fixed_assets: Dict[str, Optional[dict]] = {}
    swap_rights = GAME_STATE.setdefault("swap_rights", {})
    fixed_assets = GAME_STATE.setdefault("fixed_assets", {})

    try:
        acquired_date = (trade_date or get_current_date_as_date()).isoformat()
        season_year = int(GAME_STATE.get("league", {}).get("season_year") or 0)
        if season_year <= 0:
            try:
                season_year = int(get_current_date_as_date().year)
            except Exception:
                season_year = 0
        season_key = str(season_year) if season_year > 0 else None
        for player_id in player_ids:
            try:
                original_teams[player_id] = str(ROSTER_DF.at[player_id, "Team"]).upper()
            except Exception:
                original_teams[player_id] = ""

        for from_team, assets in deal.legs.items():
            for asset in assets:
                to_team = _resolve_receiver(deal, from_team, asset)
                if isinstance(asset, PlayerAsset):
                    if asset.player_id not in ROSTER_DF.index:
                        raise KeyError(f"Player {asset.player_id} not found")
                    ROSTER_DF.at[asset.player_id, "Team"] = to_team
                    player_state = GAME_STATE.get("players", {}).get(asset.player_id)
                    if player_state is not None:
                        if asset.player_id not in original_trade_return_bans:
                            original_trade_return_bans[asset.player_id] = deepcopy(
                                player_state.get("trade_return_bans")
                            )
                        player_state["team_id"] = to_team
                        player_state.setdefault("signed_date", "1900-01-01")
                        player_state.setdefault("signed_via_free_agency", False)
                        player_state["acquired_date"] = acquired_date
                        player_state["acquired_via_trade"] = True
                        if season_key:
                            trade_return_bans = player_state.setdefault(
                                "trade_return_bans", {}
                            )
                            season_bans = trade_return_bans.get(season_key)
                            if not isinstance(season_bans, list):
                                season_bans = []
                            if str(from_team) not in season_bans:
                                season_bans.append(str(from_team))
                            trade_return_bans[season_key] = season_bans
                if isinstance(asset, PickAsset):
                    draft_picks = GAME_STATE.get("draft_picks", {})
                    pick = draft_picks.get(asset.pick_id)
                    if pick and asset.pick_id not in original_pick_owners:
                        original_pick_owners[asset.pick_id] = str(
                            pick.get("owner_team", "")
                        ).upper()
                    if pick and asset.pick_id not in original_pick_protections:
                        original_pick_protections[asset.pick_id] = pick.get("protection")
                    if not pick:
                        raise TradeError(
                            PICK_NOT_OWNED,
                            "Pick not found",
                            {"pick_id": asset.pick_id, "team_id": from_team},
                        )
                    if asset.protection is not None:
                        if pick.get("protection") is None:
                            pick["protection"] = asset.protection
                        elif pick.get("protection") != asset.protection:
                            raise TradeError(
                                PROTECTION_CONFLICT,
                                "Pick protection conflicts with existing record",
                                {
                                    "pick_id": asset.pick_id,
                                    "existing_protection": pick.get("protection"),
                                    "attempted_protection": asset.protection,
                                },
                            )
                    transfer_pick(GAME_STATE, asset.pick_id, from_team, to_team)
                if isinstance(asset, FixedAsset):
                    fixed = fixed_assets.get(asset.asset_id)
                    if not fixed:
                        raise TradeError(
                            FIXED_ASSET_NOT_FOUND,
                            "Fixed asset not found",
                            {"asset_id": asset.asset_id, "team_id": from_team},
                        )
                    if asset.asset_id not in original_fixed_assets:
                        original_fixed_assets[asset.asset_id] = deepcopy(fixed)
                    if str(fixed.get("owner_team", "")).upper() != from_team:
                        raise TradeError(
                            FIXED_ASSET_NOT_OWNED,
                            "Fixed asset not owned by team",
                            {"asset_id": asset.asset_id, "team_id": from_team},
                        )
                    fixed["owner_team"] = to_team.upper()
                if isinstance(asset, SwapAsset):
                    draft_picks = GAME_STATE.get("draft_picks", {})
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
                    if asset.swap_id not in original_swap_rights:
                        original_swap_rights[asset.swap_id] = deepcopy(
                            swap_rights.get(asset.swap_id)
                        )
                    swap = swap_rights.get(asset.swap_id)
                    if swap:
                        if str(swap.get("owner_team", "")).upper() != from_team:
                            raise TradeError(
                                SWAP_NOT_OWNED,
                                "Swap right not owned by team",
                                {"swap_id": asset.swap_id, "team_id": from_team},
                            )
                        swap["owner_team"] = to_team.upper()
                    else:
                        swap_rights[asset.swap_id] = {
                            "swap_id": asset.swap_id,
                            "pick_id_a": asset.pick_id_a,
                            "pick_id_b": asset.pick_id_b,
                            "year": pick_a.get("year"),
                            "round": pick_a.get("round"),
                            "owner_team": to_team.upper(),
                            "active": True,
                            "created_by_deal_id": deal_id,
                            "created_at": acquired_date,
                        }

        transaction = append_trade_transaction(deal, source=source, deal_id=deal_id)
        from contracts.store import get_league_season_year
        from contracts.sync import (
            sync_contract_team_ids_from_players,
            sync_players_salary_from_active_contract,
            sync_roster_salaries_for_season,
            sync_roster_teams_from_state,
        )

        season_year = get_league_season_year(GAME_STATE)
        sync_contract_team_ids_from_players(GAME_STATE)
        sync_players_salary_from_active_contract(GAME_STATE, season_year)
        sync_roster_teams_from_state(GAME_STATE)
        sync_roster_salaries_for_season(GAME_STATE, season_year)
        return transaction
    except TradeError:
        for player_id, team_id in original_teams.items():
            if player_id in ROSTER_DF.index:
                ROSTER_DF.at[player_id, "Team"] = team_id
            player_state = GAME_STATE.get("players", {}).get(player_id)
            if player_state is not None:
                player_state["team_id"] = team_id
                if player_id in original_trade_return_bans:
                    original_bans = original_trade_return_bans[player_id]
                    if original_bans is None:
                        player_state.pop("trade_return_bans", None)
                    else:
                        player_state["trade_return_bans"] = original_bans
        draft_picks = GAME_STATE.get("draft_picks", {})
        for pick_id, owner_team in original_pick_owners.items():
            pick = draft_picks.get(pick_id)
            if pick is not None:
                pick["owner_team"] = owner_team
        for pick_id, protection in original_pick_protections.items():
            pick = draft_picks.get(pick_id)
            if pick is not None:
                pick["protection"] = protection
        for swap_id, snapshot in original_swap_rights.items():
            if snapshot is None:
                swap_rights.pop(swap_id, None)
            else:
                swap_rights[swap_id] = snapshot
        for asset_id, snapshot in original_fixed_assets.items():
            if snapshot is None:
                fixed_assets.pop(asset_id, None)
            else:
                fixed_assets[asset_id] = snapshot
        raise
    except Exception as exc:
        for player_id, team_id in original_teams.items():
            if player_id in ROSTER_DF.index:
                ROSTER_DF.at[player_id, "Team"] = team_id
            player_state = GAME_STATE.get("players", {}).get(player_id)
            if player_state is not None:
                player_state["team_id"] = team_id
                if player_id in original_trade_return_bans:
                    original_bans = original_trade_return_bans[player_id]
                    if original_bans is None:
                        player_state.pop("trade_return_bans", None)
                    else:
                        player_state["trade_return_bans"] = original_bans
        draft_picks = GAME_STATE.get("draft_picks", {})
        for pick_id, owner_team in original_pick_owners.items():
            pick = draft_picks.get(pick_id)
            if pick is not None:
                pick["owner_team"] = owner_team
        for pick_id, protection in original_pick_protections.items():
            pick = draft_picks.get(pick_id)
            if pick is not None:
                pick["protection"] = protection
        for swap_id, snapshot in original_swap_rights.items():
            if snapshot is None:
                swap_rights.pop(swap_id, None)
            else:
                swap_rights[swap_id] = snapshot
        for asset_id, snapshot in original_fixed_assets.items():
            if snapshot is None:
                fixed_assets.pop(asset_id, None)
            else:
                fixed_assets[asset_id] = snapshot
        raise TradeError(APPLY_FAILED, "Failed to apply trade", {"error": str(exc)}) from exc
