from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping

from ...errors import DEAL_INVALIDATED, TradeError
from ...models import PickAsset, resolve_asset_receiver
from ..base import TradeContext
from ..policies.stepien_policy import (
    check_stepien_violation,
    compute_max_first_round_year_in_data,
    normalize_team_id,
)


@dataclass
class PickRulesRule:
    rule_id: str = "pick_rules"
    priority: int = 80
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:

        trade_rules = ctx.game_state.get("league", {}).get("trade_rules", {})
        max_pick_years_ahead = int(trade_rules.get("max_pick_years_ahead") or 7)
        stepien_lookahead = int(trade_rules.get("stepien_lookahead") or 7)

        league = ctx.game_state.get("league", {})
        try:
            current_season_year = int(league.get("draft_year") or 0)
        except (TypeError, ValueError):
            current_season_year = 0
        if current_season_year <= 0:
            raise TradeError(
                DEAL_INVALIDATED,
                "Missing league draft_year",
                {
                    "rule": self.rule_id,
                    "reason": "missing_draft_year",
                },
            )

        assets_snapshot = _get_assets_snapshot(ctx)
        draft_picks = assets_snapshot.get("draft_picks") or {}
        if not isinstance(draft_picks, dict):
            draft_picks = {}
        # Used only for evidence payload when reporting Stepien violations.
        max_first_round_year_in_data = compute_max_first_round_year_in_data(draft_picks)

        for assets in deal.legs.values():
            for asset in assets:
                if not isinstance(asset, PickAsset):
                    continue
                pick = draft_picks.get(asset.pick_id)
                if not pick:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Pick not found",
                        {
                            "rule": self.rule_id,
                            "pick_id": asset.pick_id,
                            "reason": "missing_pick",
                        },
                    )
                pick_year = int(pick.get("year") or 0)
                if pick_year > current_season_year + max_pick_years_ahead:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Pick too far in future",
                        {
                            "rule": self.rule_id,
                            "pick_id": asset.pick_id,
                            "reason": "pick_too_far",
                            "year": pick_year,
                            "current_season_year": current_season_year,
                            "max_pick_years_ahead": max_pick_years_ahead,
                        },
                    )

        owner_after: Dict[str, str] = {
            pick_id: normalize_team_id(pick.get("owner_team"))
            for pick_id, pick in draft_picks.items()
        }
        for team_id, assets in deal.legs.items():
            for asset in assets:
                if not isinstance(asset, PickAsset):
                    continue
                receiver = resolve_asset_receiver(deal, team_id, asset)
                owner_after[asset.pick_id] = normalize_team_id(receiver)

        if stepien_lookahead <= 0:
            return

        for team_id in deal.teams:
            violation = check_stepien_violation(
                team_id=team_id,
                draft_picks=draft_picks,  # type: ignore[arg-type]
                current_draft_year=current_season_year,
                lookahead=stepien_lookahead,
                owner_after=owner_after,
            )
            if violation is not None:
                raise TradeError(
                    DEAL_INVALIDATED,
                    "Stepien rule violation",
                    {
                        "rule": self.rule_id,
                        "team_id": team_id,
                        "reason": "stepien_violation",
                        "trade_date": ctx.current_date.isoformat(),
                        "year": violation.year,
                        "next_year": violation.next_year,
                        "count_year": violation.count_year,
                        "count_next_year": violation.count_next_year,
                        "lookahead": stepien_lookahead,
                        "data_max_first_round_year": max_first_round_year_in_data,
                    },
                )


def _get_assets_snapshot(ctx: TradeContext) -> Dict[str, Any]:
    cached = ctx.extra.get("assets_snapshot")
    if isinstance(cached, dict):
        return cached  # type: ignore[return-value]

    try:
        snap = ctx.repo.get_trade_assets_snapshot()
    except Exception:
        # Fallback keeps validation usable even if a minimal repo implementation lacks
        # the combined snapshot method.
        snap = {
            "draft_picks": getattr(ctx.repo, "get_draft_picks_map", lambda: {})(),
            "swap_rights": getattr(ctx.repo, "get_swap_rights_map", lambda: {})(),
            "fixed_assets": getattr(ctx.repo, "get_fixed_assets_map", lambda: {})(),
        }

    ctx.extra["assets_snapshot"] = snap
    return snap
