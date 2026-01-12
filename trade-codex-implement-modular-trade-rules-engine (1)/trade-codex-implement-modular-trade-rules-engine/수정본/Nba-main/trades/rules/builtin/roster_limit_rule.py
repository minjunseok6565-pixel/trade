from __future__ import annotations

from dataclasses import dataclass

from schema import normalize_team_id

from ...errors import MISSING_TO_TEAM, ROSTER_LIMIT, TradeError
from ...models import PlayerAsset
from ..base import TradeContext


@dataclass
class RosterLimitRule:
    rule_id: str = "roster_limit"
    priority: int = 60
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        players_out: dict[str, int] = {team_id: 0 for team_id in deal.teams}
        players_in: dict[str, int] = {team_id: 0 for team_id in deal.teams}

        for team_id, assets in deal.legs.items():
            for asset in assets:
                if not isinstance(asset, PlayerAsset):
                    continue
                players_out[team_id] += 1
                receiver = self._resolve_receiver(deal, team_id, asset)
                players_in[receiver] += 1

        for team_id in deal.teams:
            tid = str(normalize_team_id(team_id, strict=True))
            current_count = len(ctx.repo.get_roster_player_ids(tid))
            new_count = current_count - players_out[team_id] + players_in[team_id]
            if new_count > 15:
                raise TradeError(
                    ROSTER_LIMIT,
                    "Roster limit exceeded",
                    {"team_id": team_id, "count": new_count},
                )

    @staticmethod
    def _resolve_receiver(deal, sender_team: str, asset: PlayerAsset) -> str:
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
