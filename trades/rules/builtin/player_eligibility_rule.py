from __future__ import annotations

from dataclasses import dataclass

from schema import normalize_player_id

from ...errors import DEAL_INVALIDATED, TradeError
from ...models import PlayerAsset
from ..base import TradeContext
from ..policies.player_ban_policy import (
    compute_aggregation_banned_until,
    compute_recent_signing_banned_until,
)


@dataclass
class PlayerEligibilityRule:
    rule_id: str = "player_eligibility"
    priority: int = 70
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        players = _require_rule_players(ctx)
        league = _require_league(ctx)
        trade_rules = league.get("trade_rules", {}) or {}
        season_year_start = _require_season_year(ctx)

        for team_id, assets in deal.legs.items():
            for asset in assets:
                if not isinstance(asset, PlayerAsset):
                    continue
                pid = _canonical_player_id(asset.player_id)
                if pid not in players:
                    raise RuntimeError(
                        "Trade rule evaluation requires SSOT-backed rule players meta; "
                        f"missing meta for player_id={pid}"
                    )
                player_state = players[pid]
                
                banned_until, ev = compute_recent_signing_banned_until(
                    player_state,
                    trade_rules=trade_rules,
                    season_year=season_year_start,
                    strict=True,
                )
                if not banned_until:
                    continue
                if ctx.current_date < banned_until:
                    signed_date = (ev or {}).get("signed_date")
                    dec15 = (ev or {}).get("dec15")
                    ban_days = (ev or {}).get("ban_days")
                    contract_action_type = (ev or {}).get("contract_action_type")
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Player recently signed or re-signed",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "player_id": pid,
                            "reason": "recent_contract_signing",
                            "trade_date": ctx.current_date.isoformat(),
                            "signed_date": signed_date.isoformat() if signed_date is not None else None,
                            "banned_until": banned_until.isoformat(),
                            "dec15": dec15.isoformat() if dec15 is not None else None,
                            "ban_days": int(ban_days) if ban_days is not None else None,
                            "contract_action_type": contract_action_type,
                        },
                    )

        for team_id in deal.teams:
            outgoing_assets = deal.legs.get(team_id, [])
            outgoing_players = [
                asset for asset in outgoing_assets if isinstance(asset, PlayerAsset)
            ]
            if len(outgoing_players) < 2:
                continue
            for asset in outgoing_players:
                pid = _canonical_player_id(asset.player_id)
                if pid not in players:
                    raise RuntimeError(
                        "Trade rule evaluation requires SSOT-backed rule players meta; "
                        f"missing meta for player_id={pid}"
                    )
                player_state = players[pid]
                banned_until, ev = compute_aggregation_banned_until(
                    player_state,
                    trade_rules=trade_rules,
                    strict=True,
                )
                if not banned_until:
                    continue
                if ctx.current_date < banned_until:
                    acquired_date = (ev or {}).get("acquired_date")
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Recently traded players cannot be aggregated",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "player_id": pid,
                            "reason": "aggregation_ban",
                            "trade_date": ctx.current_date.isoformat(),
                            "acquired_date": acquired_date.isoformat() if acquired_date is not None else None,
                        },
                    )


def _require_rule_players(ctx: TradeContext) -> dict:
    players = ctx.game_state.get("players")
    if not isinstance(players, dict):
        raise RuntimeError(
            "TradeContext missing rule players meta. "
            "build_trade_context() must inject SSOT-backed ctx.game_state['players']."
        )
    return players

def _require_league(ctx: TradeContext) -> dict:
    league = ctx.game_state.get("league")
    if not isinstance(league, dict):
        raise RuntimeError("TradeContext missing league snapshot in ctx.game_state['league'].")
    return league

def _require_season_year(ctx: TradeContext) -> int:
    league = _require_league(ctx)
    y = league.get("season_year")
    if y is None:
        raise RuntimeError("league.season_year missing in trade context snapshot (SSOT required).")
    try:
        yi = int(y)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"league.season_year invalid: {y!r}") from exc
    if yi <= 0:
        raise RuntimeError(f"league.season_year invalid (<=0): {yi}")
    return yi

def _canonical_player_id(value: object) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))

