from __future__ import annotations

from dataclasses import dataclass

from config import ALL_TEAM_IDS

from ...errors import INVALID_TEAM, MISSING_TO_TEAM, TradeError
from ...models import Deal
from ..base import TradeContext


@dataclass
class TeamLegsRule:
    rule_id: str = "team_legs"
    priority: int = 20
    enabled: bool = True

    def validate(self, deal: Deal, ctx: TradeContext) -> None:
        for team_id in deal.teams:
            if team_id not in ALL_TEAM_IDS:
                raise TradeError(INVALID_TEAM, f"Invalid team {team_id}")

        if set(deal.legs.keys()) != set(deal.teams):
            raise TradeError(
                INVALID_TEAM,
                "Deal legs must match deal teams",
                {"legs": list(deal.legs.keys()), "teams": list(deal.teams)},
            )

        if len(deal.teams) >= 3:
            for team_id, assets in deal.legs.items():
                for asset in assets:
                    if not asset.to_team:
                        raise TradeError(
                            MISSING_TO_TEAM,
                            "Missing to_team for multi-team deal asset",
                            {"team_id": team_id, "asset": asset},
                        )
                    if asset.to_team not in deal.teams:
                        raise TradeError(
                            INVALID_TEAM,
                            "Receiver team not in deal",
                            {"team_id": team_id, "to_team": asset.to_team},
                        )
                    if asset.to_team == team_id:
                        raise TradeError(
                            INVALID_TEAM,
                            "Receiver team cannot match sender",
                            {"team_id": team_id, "to_team": asset.to_team},
                        )

        for team_id, assets in deal.legs.items():
            for asset in assets:
                if asset.to_team:
                    if asset.to_team not in deal.teams:
                        raise TradeError(
                            INVALID_TEAM,
                            "Receiver team not in deal",
                            {"team_id": team_id, "to_team": asset.to_team},
                        )
                    if asset.to_team == team_id:
                        raise TradeError(
                            INVALID_TEAM,
                            "Receiver team cannot match sender",
                            {"team_id": team_id, "to_team": asset.to_team},
                        )
