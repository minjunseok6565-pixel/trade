from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any, Dict, List, Optional

from config import ROSTER_DF
from team_utils import _init_players_and_teams_if_needed
from state import GAME_STATE, get_current_date_as_date

from .errors import TradeError, APPLY_FAILED
from .models import Deal, PlayerAsset, PickAsset
from .picks import transfer_pick
from .transaction_log import append_trade_transaction


def _collect_player_ids(deal: Deal) -> List[int]:
    player_ids: List[int] = []
    for assets in deal.legs.values():
        for asset in assets:
            if isinstance(asset, PlayerAsset):
                player_ids.append(asset.player_id)
    return player_ids


def _resolve_receiver(deal: Deal, sender_team: str, asset: PlayerAsset | PickAsset) -> str:
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
                    transfer_pick(GAME_STATE, asset.pick_id, from_team, to_team)

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
        raise TradeError(APPLY_FAILED, "Failed to apply trade", {"error": str(exc)}) from exc
