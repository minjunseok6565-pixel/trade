from __future__ import annotations

from dataclasses import dataclass

from schema import normalize_player_id

from ...errors import DEAL_INVALIDATED, TradeError
from ...models import PlayerAsset, resolve_asset_receiver
from ..base import TradeContext


@dataclass
class ReturnToTradingTeamRule:
    rule_id: str = "return_to_trading_team_same_season"
    # Run after player eligibility but before salary matching.
    priority: int = 72
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        players = _require_rule_players(ctx)
        season_year = _require_season_year(ctx)
        season_key = str(season_year)

        for from_team, assets in deal.legs.items():
            for asset in assets:
                if not isinstance(asset, PlayerAsset):
                    continue
                to_team = resolve_asset_receiver(deal, from_team, asset)
                to_team_u = str(to_team).upper()
                pid = _canonical_player_id(asset.player_id)
                if pid not in players:
                    raise RuntimeError(
                        "Trade rule evaluation requires SSOT-backed rule players meta; "
                        f"missing meta for player_id={pid}"
                    )
                player_state = players[pid]
                # Phase2 policy:
                # - trade_return_bans key must exist and must be a dict (SSOT-derived).
                # - missing season_key implies "no bans this season" (empty list), not an error.
                if "trade_return_bans" not in player_state:
                    raise RuntimeError("Rule player meta missing required key: trade_return_bans")
                trb = player_state.get("trade_return_bans")
                if not isinstance(trb, dict):
                    raise RuntimeError(
                        f"Rule player meta key trade_return_bans must be dict, got {type(trb).__name__}"
                    )
                banned = trb.get(season_key, [])
                if banned is None:
                    raise RuntimeError("trade_return_bans season entry must be list (got None)")
                if not isinstance(banned, list):
                    raise RuntimeError(
                        f"trade_return_bans[{season_key}] must be list, got {type(banned).__name__}"
                    )
                banned_u = {str(t).upper() for t in banned if t is not None and str(t).strip()}

                if to_team_u in banned_u:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Player cannot return to trading team in same season",
                        {
                            "rule": self.rule_id,
                            "player_id": pid,
                            "from_team": from_team,
                            "to_team": to_team_u,
                            "season_year": season_year,
                            "reason": "same_season_return_to_trading_team",
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

def _require_season_year(ctx: TradeContext) -> int:
    league = ctx.game_state.get("league")
    if not isinstance(league, dict):
        raise RuntimeError("TradeContext missing league snapshot in ctx.game_state['league'].")
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

