from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id
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


@dataclass(frozen=True)
class _PlayerMove:
    player_id: str
    from_team: str
    to_team: str


def _resolve_receiver(deal: Deal, sender_team: str, asset: Any) -> str:
    if asset.to_team:
        return asset.to_team
    if len(deal.teams) == 2:
        other_team = [team for team in deal.teams if team != sender_team]
        if other_team:
            return other_team[0]
    raise TradeError(APPLY_FAILED, "Missing to_team for multi-team deal asset")


def _get_db_path(game_state: dict) -> str:
    league_state = game_state.get("league") or {}
    db_path = league_state.get("db_path")
    if not db_path:
        raise ValueError("game_state['league']['db_path'] is required to apply trades")
    return db_path


def _normalize_player_id_str(value: Any) -> str:
    return str(normalize_player_id(value, strict=True))


def _normalize_team_id_str(value: Any) -> str:
    return str(normalize_team_id(value, strict=True))


def _collect_player_moves(deal: Deal) -> list[_PlayerMove]:
    moves: list[_PlayerMove] = []
    seen: set[str] = set()
    for from_team, assets in deal.legs.items():
        normalized_from_team = _normalize_team_id_str(from_team)
        for asset in assets:
            if not isinstance(asset, PlayerAsset):
                continue
            player_id = _normalize_player_id_str(asset.player_id)
            if player_id in seen:
                raise ValueError(f"duplicate player in trade assets: {player_id}")
            seen.add(player_id)
            to_team = _resolve_receiver(deal, normalized_from_team, asset)
            normalized_to_team = _normalize_team_id_str(to_team)
            moves.append(
                _PlayerMove(
                    player_id=player_id,
                    from_team=normalized_from_team,
                    to_team=normalized_to_team,
                )
            )
    return moves


def _validate_player_moves(repo: LeagueRepo, moves: list[_PlayerMove]) -> None:
    for move in moves:
        try:
            current_team = repo.get_team_id_by_player(move.player_id)
        except KeyError as exc:
            raise ValueError(
                f"player_id not found in DB: {move.player_id}"
            ) from exc
        if current_team != move.from_team:
            raise ValueError(
                "player_id "
                f"{move.player_id} expected team {move.from_team} "
                f"but DB shows {current_team}"
            )


def apply_deal(
    game_state: dict | Deal,
    deal: Deal | None = None,
    source: str = "",
    deal_id: Optional[str] = None,
    trade_date: Optional[date] = None,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if deal is None:
        if isinstance(game_state, Deal):
            deal = game_state
            game_state = GAME_STATE
        else:
            raise TypeError("apply_deal requires a Deal")

    _init_players_and_teams_if_needed()
    original_player_teams: Dict[str, Optional[str]] = {}
    original_trade_return_bans: Dict[str, Optional[Dict[str, List[str]]]] = {}
    original_pick_owners: Dict[str, str] = {}
    original_pick_protections: Dict[str, Any] = {}
    original_swap_rights: Dict[str, Optional[dict]] = {}
    original_fixed_assets: Dict[str, Optional[dict]] = {}
    swap_rights = game_state.setdefault("swap_rights", {})
    fixed_assets = game_state.setdefault("fixed_assets", {})
    player_moves = _collect_player_moves(deal)
    normalized_player_ids = [move.player_id for move in player_moves]

    try:
        acquired_date = (trade_date or get_current_date_as_date()).isoformat()
        season_year_raw = game_state.get("league", {}).get("season_year")
        season_key = str(season_year_raw).strip() if season_year_raw is not None else ""
        if not season_key.isdigit():
            season_key = str(get_current_date_as_date().year)
        if not season_key.isdigit():
            season_key = None
        db_path = _get_db_path(game_state)

        with LeagueRepo(db_path) as repo:
            repo.init_db()
            _validate_player_moves(repo, player_moves)
            if dry_run:
                return {
                    "dry_run": True,
                    "player_moves": [move.__dict__ for move in player_moves],
                }
            with repo.transaction():
                for move in player_moves:
                    repo.trade_player(move.player_id, move.to_team)
            repo.validate_integrity()

        for from_team, assets in deal.legs.items():
            normalized_from_team = _normalize_team_id_str(from_team)
            for asset in assets:
                to_team = _resolve_receiver(deal, normalized_from_team, asset)
                normalized_to_team = _normalize_team_id_str(to_team)
                if isinstance(asset, PlayerAsset):
                    player_id = _normalize_player_id_str(asset.player_id)
                    player_state = game_state.get("players", {}).get(player_id)
                    if player_state is not None:
                        if player_id not in original_player_teams:
                            original_player_teams[player_id] = player_state.get("team_id")
                        if player_id not in original_trade_return_bans:
                            original_trade_return_bans[player_id] = deepcopy(
                                player_state.get("trade_return_bans")
                            )
                        player_state["team_id"] = normalized_to_team
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
                            if normalized_from_team not in season_bans:
                                season_bans.append(normalized_from_team)
                            trade_return_bans[season_key] = season_bans
                if isinstance(asset, PickAsset):
                    draft_picks = game_state.get("draft_picks", {})
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
                            {"pick_id": asset.pick_id, "team_id": normalized_from_team},
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
                    transfer_pick(game_state, asset.pick_id, normalized_from_team, normalized_to_team)
                if isinstance(asset, FixedAsset):
                    fixed = fixed_assets.get(asset.asset_id)
                    if not fixed:
                        raise TradeError(
                            FIXED_ASSET_NOT_FOUND,
                            "Fixed asset not found",
                            {"asset_id": asset.asset_id, "team_id": normalized_from_team},
                        )
                    if asset.asset_id not in original_fixed_assets:
                        original_fixed_assets[asset.asset_id] = deepcopy(fixed)
                    if str(fixed.get("owner_team", "")).upper() != normalized_from_team:
                        raise TradeError(
                            FIXED_ASSET_NOT_OWNED,
                            "Fixed asset not owned by team",
                            {"asset_id": asset.asset_id, "team_id": normalized_from_team},
                        )
                    fixed["owner_team"] = normalized_to_team
                if isinstance(asset, SwapAsset):
                    draft_picks = game_state.get("draft_picks", {})
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
                        if str(swap.get("owner_team", "")).upper() != normalized_from_team:
                            raise TradeError(
                                SWAP_NOT_OWNED,
                                "Swap right not owned by team",
                                {"swap_id": asset.swap_id, "team_id": normalized_from_team},
                            )
                        swap["owner_team"] = normalized_to_team
                    else:
                        swap_rights[asset.swap_id] = {
                            "swap_id": asset.swap_id,
                            "pick_id_a": asset.pick_id_a,
                            "pick_id_b": asset.pick_id_b,
                            "year": pick_a.get("year"),
                            "round": pick_a.get("round"),
                            "owner_team": normalized_to_team,
                            "active": True,
                            "created_by_deal_id": deal_id,
                            "created_at": acquired_date,
                        }

        transaction = append_trade_transaction(deal, source=source, deal_id=deal_id)
        return transaction
    except TradeError:
        for player_id in normalized_player_ids:
            player_state = game_state.get("players", {}).get(player_id)
            if player_state is not None:
                if player_id in original_player_teams:
                    player_state["team_id"] = original_player_teams[player_id]
                if player_id in original_trade_return_bans:
                    original_bans = original_trade_return_bans[player_id]
                    if original_bans is None:
                        player_state.pop("trade_return_bans", None)
                    else:
                        player_state["trade_return_bans"] = original_bans
        draft_picks = game_state.get("draft_picks", {})
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
        for player_id in normalized_player_ids:
            player_state = game_state.get("players", {}).get(player_id)
            if player_state is not None:
                if player_id in original_player_teams:
                    player_state["team_id"] = original_player_teams[player_id]
                if player_id in original_trade_return_bans:
                    original_bans = original_trade_return_bans[player_id]
                    if original_bans is None:
                        player_state.pop("trade_return_bans", None)
                    else:
                        player_state["trade_return_bans"] = original_bans
        draft_picks = game_state.get("draft_picks", {})
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
