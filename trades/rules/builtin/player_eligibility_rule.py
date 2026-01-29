from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from schema import normalize_player_id

from ...errors import DEAL_INVALIDATED, TradeError
from ...models import PlayerAsset
from ..base import TradeContext


@dataclass
class PlayerEligibilityRule:
    rule_id: str = "player_eligibility"
    priority: int = 70
    enabled: bool = True

    def validate(self, deal, ctx: TradeContext) -> None:
        players = _require_rule_players(ctx)
        league = _require_league(ctx)
        trade_rules = league.get("trade_rules", {})
        new_fa_sign_ban_days = int(trade_rules.get("new_fa_sign_ban_days") or 90)
        aggregation_ban_days = int(trade_rules.get("aggregation_ban_days") or 60)

        season_year_start = _require_season_year(ctx)
        dec15 = date(season_year_start, 12, 15)

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
                contract_action_type = _require_str_key(player_state, "last_contract_action_type", allow_none=True)
                signed_via_fa = _require_bool_key(player_state, "signed_via_free_agency")
                is_recent_signing = contract_action_type in {
                    "SIGN_FREE_AGENT",
                    "RE_SIGN_OR_EXTEND",
                }
                if not is_recent_signing and not signed_via_fa:
                    continue

                # Phase2 policy: if the rule applies, required dates must exist; no silent defaults.
                signed_date_value = (
                    player_state.get("last_contract_action_date")
                    if player_state.get("last_contract_action_date") is not None
                    else player_state.get("signed_date")
                )
                signed_date = _parse_required_date(
                    signed_date_value, f"player_id={pid} signed_date/last_contract_action_date"
                )
                banned_until_days = signed_date + timedelta(days=new_fa_sign_ban_days)
                banned_until = max(banned_until_days, dec15)
                if ctx.current_date < banned_until:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Player recently signed or re-signed",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "player_id": pid,
                            "reason": "recent_contract_signing",
                            "trade_date": ctx.current_date.isoformat(),
                            "signed_date": signed_date.isoformat(),
                            "banned_until": banned_until.isoformat(),
                            "dec15": dec15.isoformat(),
                            "ban_days": new_fa_sign_ban_days,
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
                acquired_via_trade = _require_bool_key(player_state, "acquired_via_trade")
                if not acquired_via_trade:
                    continue
                acquired_date = _parse_required_date(
                    player_state.get("acquired_date"), f"player_id={pid} acquired_date"
                )
                banned_until = acquired_date + timedelta(days=aggregation_ban_days)
                if ctx.current_date < banned_until:
                    raise TradeError(
                        DEAL_INVALIDATED,
                        "Recently traded players cannot be aggregated",
                        {
                            "rule": self.rule_id,
                            "team_id": team_id,
                            "player_id": pid,
                            "reason": "aggregation_ban",
                            "trade_date": ctx.current_date.isoformat(),
                            "acquired_date": acquired_date.isoformat(),
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

def _require_bool_key(player_state: dict, key: str) -> bool:
    if key not in player_state:
        raise RuntimeError(f"Rule player meta missing required key: {key}")
    v = player_state.get(key)
    if not isinstance(v, bool):
        raise RuntimeError(f"Rule player meta key {key} must be bool, got {type(v).__name__}")
    return v

def _require_str_key(player_state: dict, key: str, *, allow_none: bool = False) -> str | None:
    if key not in player_state:
        raise RuntimeError(f"Rule player meta missing required key: {key}")
    v = player_state.get(key)
    if v is None and allow_none:
        return None
    if not isinstance(v, str):
        raise RuntimeError(f"Rule player meta key {key} must be str, got {type(v).__name__}")
    return v


def _canonical_player_id(value: object) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))
    

def _parse_required_date(value: object, context: str) -> date:
    """Parse ISO date/datetime string into a date. Fail-fast if missing/unparseable."""
    if value is None:
        raise RuntimeError(f"Required date missing for {context}")
    s = str(value).strip()
    if len(s) < 10:
        raise RuntimeError(f"Required date invalid for {context}: {value!r}")
    try:
        return date.fromisoformat(s[:10])
    except ValueError as exc:
        raise RuntimeError(f"Required date unparseable for {context}: {value!r}") from exc

