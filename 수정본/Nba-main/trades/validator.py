from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from config import ALL_TEAM_IDS, ROSTER_DF
from state import GAME_STATE, _ensure_league_state, get_current_date_as_date
from salary_cap import HARD_CAP, compute_payroll_after_player_moves

from .errors import (
    TradeError,
    TRADE_DEADLINE_PASSED,
    INVALID_TEAM,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    ROSTER_LIMIT,
    HARD_CAP_EXCEEDED,
    ASSET_LOCKED,
    MISSING_TO_TEAM,
    DUPLICATE_ASSET,
)
from .models import Deal, PlayerAsset, PickAsset


def _asset_key(asset: PlayerAsset | PickAsset) -> str:
    if isinstance(asset, PlayerAsset):
        return f"player:{asset.player_id}"
    return f"pick:{asset.pick_id}"


def _resolve_receiver(deal: Deal, sender_team: str, asset: PlayerAsset | PickAsset) -> str:
    if asset.to_team:
        return asset.to_team
    if len(deal.teams) == 2:
        other_team = [team for team in deal.teams if team != sender_team]
        if other_team:
            return other_team[0]
    raise TradeError(
        MISSING_TO_TEAM,
        "Missing to_team for multi-team deal asset",
        {"team_id": sender_team, "asset": asset},
    )


def validate_deal(
    deal: Deal,
    current_date: Optional[date] = None,
    allow_locked_by_deal_id: Optional[str] = None,
) -> None:
    league = _ensure_league_state()
    trade_deadline = league.get("trade_rules", {}).get("trade_deadline")
    if trade_deadline:
        deadline_date = date.fromisoformat(str(trade_deadline))
        today = current_date or get_current_date_as_date()
        if today > deadline_date:
            raise TradeError(
                TRADE_DEADLINE_PASSED,
                "Trade deadline has passed",
                {"deadline": trade_deadline},
            )

    seen_assets: Dict[str, str] = {}
    for team_id, assets in deal.legs.items():
        for asset in assets:
            asset_key = _asset_key(asset)
            if asset_key in seen_assets:
                raise TradeError(
                    DUPLICATE_ASSET,
                    "Duplicate asset in deal",
                    {
                        "asset_key": asset_key,
                        "first_sender": seen_assets[asset_key],
                        "duplicate_sender": team_id,
                    },
                )
            seen_assets[asset_key] = team_id

    for team_id in deal.teams:
        if team_id not in ALL_TEAM_IDS:
            raise TradeError(INVALID_TEAM, f"Invalid team {team_id}")

    asset_locks = GAME_STATE.get("asset_locks", {})
    for team_id, assets in deal.legs.items():
        for asset in assets:
            asset_key = _asset_key(asset)
            lock = asset_locks.get(asset_key)
            if not lock:
                continue
            locked_deal_id = lock.get("deal_id")
            expires_at = lock.get("expires_at")
            if expires_at is not None:
                try:
                    if isinstance(expires_at, date):
                        expires_at_date = expires_at
                    else:
                        expires_at_date = date.fromisoformat(str(expires_at))
                except ValueError:
                    raise TradeError(
                        ASSET_LOCKED,
                        "Asset lock expiry could not be parsed",
                        {
                            "asset_key": asset_key,
                            "deal_id": locked_deal_id,
                            "expires_at": expires_at,
                        },
                    )
                today = current_date or get_current_date_as_date()
                if today > expires_at_date:
                    asset_locks.pop(asset_key, None)
                    continue
            if allow_locked_by_deal_id and locked_deal_id == allow_locked_by_deal_id:
                continue
            raise TradeError(
                ASSET_LOCKED,
                "Asset is locked",
                {"asset_key": asset_key, "deal_id": locked_deal_id},
            )

        for asset in assets:
            if isinstance(asset, PlayerAsset):
                try:
                    current_team = str(ROSTER_DF.at[asset.player_id, "Team"]).upper()
                except Exception:
                    current_team = ""
                if current_team != team_id:
                    raise TradeError(
                        PLAYER_NOT_OWNED,
                        "Player not owned by team",
                        {"player_id": asset.player_id, "team_id": team_id},
                    )
            if isinstance(asset, PickAsset):
                draft_picks = GAME_STATE.get("draft_picks", {})
                pick = draft_picks.get(asset.pick_id)
                if not pick:
                    raise TradeError(
                        PICK_NOT_OWNED,
                        "Pick not found",
                        {"pick_id": asset.pick_id, "team_id": team_id},
                    )
                if str(pick.get("owner_team", "")).upper() != team_id:
                    raise TradeError(
                        PICK_NOT_OWNED,
                        "Pick not owned by team",
                        {"pick_id": asset.pick_id, "team_id": team_id},
                    )

    roster_counts: Dict[str, int] = {
        team_id: int((ROSTER_DF["Team"] == team_id).sum()) for team_id in deal.teams
    }
    players_out: Dict[str, List[int]] = {team_id: [] for team_id in deal.teams}
    players_in: Dict[str, List[int]] = {team_id: [] for team_id in deal.teams}

    for team_id, assets in deal.legs.items():
        for asset in assets:
            if isinstance(asset, PlayerAsset):
                players_out[team_id].append(asset.player_id)
                receiver = _resolve_receiver(deal, team_id, asset)
                if receiver not in deal.teams:
                    raise TradeError(
                        INVALID_TEAM,
                        "Receiver team not in deal",
                        {"team_id": team_id, "to_team": receiver},
                    )
                if receiver == team_id:
                    raise TradeError(
                        INVALID_TEAM,
                        "Receiver team cannot match sender",
                        {"team_id": team_id, "to_team": receiver},
                    )
                players_in[receiver].append(asset.player_id)
            if isinstance(asset, PickAsset):
                receiver = _resolve_receiver(deal, team_id, asset)
                if receiver not in deal.teams:
                    raise TradeError(
                        INVALID_TEAM,
                        "Receiver team not in deal",
                        {"team_id": team_id, "to_team": receiver},
                    )
                if receiver == team_id:
                    raise TradeError(
                        INVALID_TEAM,
                        "Receiver team cannot match sender",
                        {"team_id": team_id, "to_team": receiver},
                    )

    for team_id in deal.teams:
        new_count = roster_counts[team_id] - len(players_out[team_id]) + len(players_in[team_id])
        if new_count > 15:
            raise TradeError(
                ROSTER_LIMIT,
                "Roster limit exceeded",
                {"team_id": team_id, "count": new_count},
            )

        payroll_after = compute_payroll_after_player_moves(
            team_id,
            players_out[team_id],
            players_in[team_id],
        )
        if payroll_after > HARD_CAP:
            raise TradeError(
                HARD_CAP_EXCEEDED,
                "Hard cap exceeded",
                {"team_id": team_id, "payroll": payroll_after, "hard_cap": HARD_CAP},
            )


if __name__ == "__main__":
    try:
        sample = Deal(
            teams=["AAA", "BBB"],
            legs={
                "AAA": [PlayerAsset(kind="player", player_id=0)],
                "BBB": [],
            },
        )
        validate_deal(sample)
    except TradeError as exc:
        print(exc)
