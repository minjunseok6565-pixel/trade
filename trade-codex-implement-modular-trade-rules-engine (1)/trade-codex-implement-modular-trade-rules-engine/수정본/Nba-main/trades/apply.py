from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id
from state import GAME_STATE, get_league_db_path

from .errors import APPLY_FAILED, TradeError
from .models import Deal, FixedAsset, PickAsset, PlayerAsset, SwapAsset
from .transaction_log import append_trade_transaction


@dataclass(frozen=True)
class _PlayerMove:
    player_id: str
    from_team: str
    to_team: str


@dataclass(frozen=True)
class _PickMove:
    pick_id: str
    from_team: str
    to_team: str
    protection: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class _SwapMove:
    swap_id: str
    from_team: str
    to_team: str


@dataclass(frozen=True)
class _FixedAssetMove:
    asset_id: str
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


def _collect_pick_moves(deal: Deal) -> list[_PickMove]:
    moves: list[_PickMove] = []
    for from_team, assets in deal.legs.items():
        normalized_from_team = _normalize_team_id_str(from_team)
        for asset in assets:
            if not isinstance(asset, PickAsset):
                continue
            to_team = _resolve_receiver(deal, normalized_from_team, asset)
            normalized_to_team = _normalize_team_id_str(to_team)
            moves.append(
                _PickMove(
                    pick_id=str(asset.pick_id),
                    from_team=normalized_from_team,
                    to_team=normalized_to_team,
                    protection=asset.protection,
                )
            )
    return moves


def _collect_swap_moves(deal: Deal) -> list[_SwapMove]:
    moves: list[_SwapMove] = []
    for from_team, assets in deal.legs.items():
        normalized_from_team = _normalize_team_id_str(from_team)
        for asset in assets:
            if not isinstance(asset, SwapAsset):
                continue
            to_team = _resolve_receiver(deal, normalized_from_team, asset)
            normalized_to_team = _normalize_team_id_str(to_team)
            moves.append(
                _SwapMove(
                    swap_id=str(asset.swap_id),
                    from_team=normalized_from_team,
                    to_team=normalized_to_team,
                )
            )
    return moves


def _collect_fixed_asset_moves(deal: Deal) -> list[_FixedAssetMove]:
    moves: list[_FixedAssetMove] = []
    for from_team, assets in deal.legs.items():
        normalized_from_team = _normalize_team_id_str(from_team)
        for asset in assets:
            if not isinstance(asset, FixedAsset):
                continue
            to_team = _resolve_receiver(deal, normalized_from_team, asset)
            normalized_to_team = _normalize_team_id_str(to_team)
            moves.append(
                _FixedAssetMove(
                    asset_id=str(asset.asset_id),
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

    player_moves = _collect_player_moves(deal)
    pick_moves = _collect_pick_moves(deal)
    swap_moves = _collect_swap_moves(deal)
    fixed_asset_moves = _collect_fixed_asset_moves(deal)

    try:
        db_path = get_league_db_path(game_state)

        with LeagueRepo(db_path) as repo:
            repo.init_db()
            _validate_player_moves(repo, player_moves)
            if dry_run:
                return {
                    "dry_run": True,
                    "player_moves": [move.__dict__ for move in player_moves],
                    "pick_moves": [move.__dict__ for move in pick_moves],
                    "swap_moves": [move.__dict__ for move in swap_moves],
                    "fixed_asset_moves": [move.__dict__ for move in fixed_asset_moves],
                }
            with repo.transaction() as cur:
                for move in player_moves:
                    repo.trade_player(move.player_id, move.to_team, cursor=cur)
                for move in pick_moves:
                    repo.update_pick_owner(move.pick_id, move.to_team, cursor=cur)
                    if move.protection is not None:
                        repo.set_pick_protection(move.pick_id, move.protection, cursor=cur)
                for move in swap_moves:
                    repo.update_swap_owner(move.swap_id, move.to_team, cursor=cur)
                for move in fixed_asset_moves:
                    repo.update_fixed_asset_owner(move.asset_id, move.to_team, cursor=cur)
            repo.validate_integrity()

        transaction = append_trade_transaction(deal, source=source, deal_id=deal_id)
        return transaction
    except TradeError:
        raise
    except Exception as exc:
        raise TradeError(APPLY_FAILED, "Failed to apply trade", {"error": str(exc)}) from exc
