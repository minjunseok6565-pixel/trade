from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ...errors import DEAL_INVALIDATED, MISSING_TO_TEAM, TradeError
from ...models import PickAsset
from ..base import TradeContext


@dataclass
class PickRulesRule:
    rule_id: str = "pick_rules"
    priority: int = 80
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        def _norm_team_id(x) -> str:
            """Normalize team ids for comparisons (e.g., owner_team="lal" == receiver="LAL")."""
            return str(x).strip().upper() if x and str(x).strip() else ""

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
        # Safety guard: Stepien rule checks (year, year+1) pairs.
        # If draft_picks data doesn't include year+1 at all (older saves / partial state),
        # a missing year would be misread as "0 picks" and can cause false violations.
        max_first_round_year_in_data = 0
        for pick in draft_picks.values():
            try:
                if int(pick.get("round") or 0) != 1:
                    continue
                year_val = int(pick.get("year") or 0)
            except (TypeError, ValueError):
                continue
            if year_val > max_first_round_year_in_data:
                max_first_round_year_in_data = year_val

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

        owner_after = {
            pick_id: _norm_team_id(pick.get("owner_team"))
            for pick_id, pick in draft_picks.items()
        }
        for team_id, assets in deal.legs.items():
            for asset in assets:
                if not isinstance(asset, PickAsset):
                    continue
                receiver = _resolve_receiver(deal, team_id, asset)
                owner_after[asset.pick_id] = _norm_team_id(receiver)

        if stepien_lookahead <= 0:
            return

        for team_id in deal.teams:
            normalized_team_id = _norm_team_id(team_id)
            start = current_season_year + 1
            end = current_season_year + stepien_lookahead
            # Clamp end so that (year+1) is still within available pick data.
            # If max_first_round_year_in_data is 0, we can't clamp safely (no data), so keep original end.
            if max_first_round_year_in_data > 0:
                end = min(end, max_first_round_year_in_data - 1)
            if end < start:
                continue            
            for year in range(start, end + 1):  # Inclusive to check (end, end + 1) pair.
                count_year = _count_first_round_picks_for_year(
                    draft_picks, owner_after, normalized_team_id, year
                )
                count_next = _count_first_round_picks_for_year(
                    draft_picks, owner_after, normalized_team_id, year + 1
                )
                if count_year == 0 and count_next == 0:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Stepien rule violation",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "reason": "stepien_violation",
                            "trade_date": ctx.current_date.isoformat(),
                            "year": year,
                            "lookahead": stepien_lookahead,
                            "data_max_first_round_year": max_first_round_year_in_data,
                        },
                    )


def _resolve_receiver(deal, team_id: str, asset: PickAsset) -> str:
    if asset.to_team:
        return asset.to_team
    if len(deal.teams) == 2:
        other_team = [team for team in deal.teams if team != team_id]
        if other_team:
            return other_team[0]
    raise TradeError(
        MISSING_TO_TEAM,
        "Missing to_team for multi-team deal asset",
        {"team_id": team_id, "asset": asset},
    )


def _count_first_round_picks_for_year(
    draft_picks: dict,
    owner_after: dict[str, str],
    team_id: str,
    year: int,
) -> int:
    count = 0
    for pick_id, pick in draft_picks.items():
        if int(pick.get("year") or 0) != year:
            continue
        if int(pick.get("round") or 0) != 1:
            continue
        if owner_after.get(pick_id) == team_id:
            count += 1
    return count


def _get_assets_snapshot(ctx: TradeContext) -> Dict[str, Any]:
    """Return a consistent DB snapshot of trade-relevant assets.

    This rule MUST NOT depend on state dicts for draft_picks/swap_rights/fixed_assets because
    those ledgers have been migrated to DB SSOT. We cache the snapshot on ctx.extra so:
      - multiple rules don't re-query the DB repeatedly
      - all rules see a consistent view during validation
    """
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
