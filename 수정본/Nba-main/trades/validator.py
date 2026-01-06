from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from config import ALL_TEAM_IDS, ROSTER_DF
from state import GAME_STATE, _ensure_league_state, get_current_date_as_date
from .errors import (
    TradeError,
    INVALID_TEAM,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    MISSING_TO_TEAM,
    DUPLICATE_ASSET,
)
from .models import Deal, PlayerAsset, PickAsset
from .rules import build_trade_context, validate_all


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
    _ensure_league_state()
    from contracts.store import get_league_season_year
    from contracts.sync import (
        sync_roster_salaries_for_season,
        sync_roster_teams_from_state,
    )

    season_year = get_league_season_year(GAME_STATE)
    sync_roster_teams_from_state(GAME_STATE)
    sync_roster_salaries_for_season(GAME_STATE, season_year)

    # RULES ENGINE CHECKS (migrated): deadline
    ctx = build_trade_context(current_date=current_date)
    validate_all(deal, ctx)

    # LEGACY CHECKS (to be migrated later): locks/ownership/roster/cap/...
    # === MIGRATE:DUPLICATE_ASSET:START ===
    # migrated to rules engine: DuplicateAssetRule
    # === MIGRATE:DUPLICATE_ASSET:END ===

    # === MIGRATE:TEAM_LEGS:START ===
    # migrated to rules engine: TeamLegsRule
    # === MIGRATE:TEAM_LEGS:END ===
    # === MIGRATE:ASSET_LOCKS:START ===
    # migrated to rules engine: AssetLockRule
    # === MIGRATE:ASSET_LOCKS:END ===

        # === MIGRATE:OWNERSHIP:START ===
        # migrated to rules engine: OwnershipRule
        # === MIGRATE:OWNERSHIP:END ===

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
        # === MIGRATE:ROSTER_LIMIT:START ===
        # migrated to rules engine: RosterLimitRule
        # === MIGRATE:ROSTER_LIMIT:END ===


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
