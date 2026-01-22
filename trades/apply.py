from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id
from team_utils import _init_players_and_teams_if_needed
import state as state_facade
from state_modules.state_core import get_current_date_as_date

from .errors import (
    APPLY_FAILED,
    TradeError,
)
from .models import Deal, FixedAsset, PickAsset, PlayerAsset, SwapAsset


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
    # adapt plain object -> context manager
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


def apply_deal(
    game_state: dict | Deal,
    deal: Deal | None = None,
    source: str = "",
    deal_id: Optional[str] = None,
    trade_date: Optional[date] = None,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    uses_state_facade = False
    if deal is None:
        if isinstance(game_state, Deal):
            deal = game_state
            game_state = state_facade.export_state()
            uses_state_facade = True
        else:
            raise TypeError("apply_deal requires a Deal")

    _init_players_and_teams_if_needed()

    player_moves = _collect_player_moves(deal)

    try:
        acquired_date = (trade_date or get_current_date_as_date()).isoformat()
        season_year_raw = game_state.get("league", {}).get("season_year")
        season_key = str(season_year_raw).strip() if season_year_raw is not None else ""
        if not season_key.isdigit():
            season_key = str(get_current_date_as_date().year)
        if not season_key.isdigit():
            season_key = None
        db_path = _get_db_path(game_state)

        # dry_run: keep existing behavior (lightweight DB checks on players only)
        if dry_run:
            with LeagueRepo(db_path) as repo:
                repo.init_db()
                _validate_player_moves(repo, player_moves)
            return {
                "dry_run": True,
                "player_moves": [m.__dict__ for m in player_moves],
            }

        # ✅ Single SSOT write: execute_trade handles players/picks/swaps/fixed_assets/log atomically
        with _open_service(db_path) as svc:
            tx = svc.execute_trade(deal, source=source, trade_date=trade_date, deal_id=deal_id)

        # Optional: update lightweight cached player fields in state (NOT assets ledger)
        players_state = game_state.get("players", {})
        if isinstance(players_state, dict):
            for move in player_moves:
                ps = players_state.get(move.player_id)
                if not isinstance(ps, dict):
                    continue
                ps["team_id"] = move.to_team
                ps.setdefault("signed_date", "1900-01-01")
                ps.setdefault("signed_via_free_agency", False)
                ps["acquired_date"] = acquired_date
                ps["acquired_via_trade"] = True
                if season_key:
                    bans = ps.setdefault("trade_return_bans", {})
                    season_bans = bans.get(season_key)
                    if not isinstance(season_bans, list):
                        season_bans = []
                    if move.from_team not in season_bans:
                        season_bans.append(move.from_team)
                    bans[season_key] = season_bans

        if uses_state_facade:
            state_facade.import_state(game_state)
        return tx
    except TradeError:
        raise
    except Exception as exc:
        raise TradeError(APPLY_FAILED, "Failed to apply trade", {"error": str(exc)}) from exc
