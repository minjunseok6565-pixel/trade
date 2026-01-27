from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id

from .errors import APPLY_FAILED, TradeError
from .models import Deal, PlayerAsset


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
            raise ValueError(f"player_id not found in DB: {move.player_id}") from exc
        if current_team != move.from_team:
            raise ValueError(
                "player_id "
                f"{move.player_id} expected team {move.from_team} "
                f"but DB shows {current_team}"
            )


def _open_service(db_path: str):
    """
    Canonical entrypoint: LeagueService.open(db_path).
    Handles both context-manager and plain-object returns.
    """
    import contextlib
    from league_service import LeagueService  # local import to avoid cycles

    svc_or_cm = LeagueService.open(db_path)
    if hasattr(svc_or_cm, "__enter__"):
        return svc_or_cm  # context manager

    @contextlib.contextmanager
    def _cm():
        svc = svc_or_cm
        try:
            yield svc
        finally:
            close = getattr(svc, "close", None)
            if callable(close):
                close()

    return _cm()


def apply_deal_to_db(
    db_path: str,
    deal: Deal,
    source: str,
    deal_id: str | None,
    trade_date,
    dry_run: bool,
) -> Dict[str, Any]:
    if not db_path:
        raise ValueError("db_path is required to apply trades")

    player_moves = _collect_player_moves(deal)

    try:
        if dry_run:
            with LeagueRepo(db_path) as repo:
                repo.init_db()
                _validate_player_moves(repo, player_moves)
            return {
                "dry_run": True,
                "player_moves": [m.__dict__ for m in player_moves],
            }

        with _open_service(db_path) as svc:
            return svc.execute_trade(deal, source=source, trade_date=trade_date, deal_id=deal_id)
    except TradeError:
        raise
    except Exception as exc:
        raise TradeError(APPLY_FAILED, "Failed to apply trade", {"error": str(exc)}) from exc
