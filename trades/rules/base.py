from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional, Protocol

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id


@dataclass
class TradeContext:
    game_state: dict
    repo: LeagueRepo
    db_path: Optional[str]
    current_date: date
    extra: dict[str, Any] = field(default_factory=dict)


class Rule(Protocol):
    rule_id: str
    priority: int
    enabled: bool

    def validate(self, deal: Any, ctx: TradeContext) -> None:
        ...


def build_player_moves(deal: Any) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    from ..models import PlayerAsset

    players_out: dict[str, list[str]] = {team_id: [] for team_id in deal.teams}
    players_in: dict[str, list[str]] = {team_id: [] for team_id in deal.teams}

    for team_id, assets in deal.legs.items():
        for asset in assets:
            if not isinstance(asset, PlayerAsset):
                continue
            player_id = _normalize_player_id(asset.player_id)
            players_out[team_id].append(player_id)
            receiver = _resolve_receiver(deal, team_id, asset)
            players_in[receiver].append(player_id)

    return players_out, players_in


def _normalize_player_id(value: Any) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))


def _normalize_team_id(value: Any) -> str:
    return str(normalize_team_id(value, strict=True))


def _sum_player_salaries(repo: LeagueRepo, player_ids: list[str]) -> float:
    if not player_ids:
        return 0.0
    total = 0.0
    for player_id in player_ids:
        pid = _normalize_player_id(player_id)
        salary = repo.get_salary_amount(pid)
        total += float(salary or 0)
    return total


def build_team_trade_totals(
    deal: Any,
    ctx: TradeContext,
) -> dict[str, dict[str, float | int]]:
    players_out, players_in = build_player_moves(deal)
    totals: dict[str, dict[str, float | int]] = {}

    for team_id in deal.teams:
        outgoing_players = players_out.get(team_id, [])
        incoming_players = players_in.get(team_id, [])
        totals[team_id] = {
            "outgoing_salary": _sum_player_salaries(ctx.repo, outgoing_players),
            "incoming_salary": _sum_player_salaries(ctx.repo, incoming_players),
            "outgoing_players_count": len(outgoing_players),
            "incoming_players_count": len(incoming_players),
        }

    return totals


def build_team_payrolls(
    deal: Any,
    ctx: TradeContext,
    trade_totals: Optional[dict[str, dict[str, float | int]]] = None,
) -> dict[str, dict[str, float]]:
    totals = trade_totals or build_team_trade_totals(deal, ctx)
    payrolls: dict[str, dict[str, float]] = {}

    for team_id in deal.teams:
        tid = _normalize_team_id(team_id)
        payroll_before = float(
            sum(float(row.get("salary_amount") or 0) for row in ctx.repo.get_team_roster(tid))
        )
        outgoing_salary = float(totals[team_id]["outgoing_salary"])
        incoming_salary = float(totals[team_id]["incoming_salary"])
        payrolls[team_id] = {
            "payroll_before": payroll_before,
            "payroll_after": payroll_before - outgoing_salary + incoming_salary,
        }

    return payrolls


def _resolve_receiver(deal: Any, sender_team: str, asset: Any) -> str:
    if getattr(asset, "to_team", None):
        return asset.to_team
    if len(deal.teams) == 2:
        other_team = [team for team in deal.teams if team != sender_team]
        if other_team:
            return other_team[0]
    from ..errors import MISSING_TO_TEAM, TradeError

    raise TradeError(
        MISSING_TO_TEAM,
        "Missing to_team for multi-team deal asset",
        {"team_id": sender_team, "asset": asset},
    )


def build_trade_context(
    deal: Any,
    current_date: Optional[date] = None,
    extra: Optional[dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> TradeContext:
    import state
    from . import rule_player_meta

    if current_date is None:
        current_date = state.get_current_date_as_date()

    resolved_extra = dict(extra) if extra else {}
    if "allow_locked_by_deal_id" not in resolved_extra:
        import inspect

        frame = inspect.currentframe()
        caller = frame.f_back if frame else None
        allow_locked_by_deal_id = caller.f_locals.get("allow_locked_by_deal_id") if caller else None
        if allow_locked_by_deal_id is not None:
            resolved_extra["allow_locked_by_deal_id"] = allow_locked_by_deal_id

    resolved_db_path = db_path or state.get_db_path()
    repo = LeagueRepo(resolved_db_path)
    # DB schema is guaranteed during server startup (state.startup_init_state()).

    ctx_state = state.export_trade_context_snapshot()
    assets_snap = state.export_trade_assets_snapshot()
    resolved_extra.setdefault("assets_snapshot", assets_snap)

    # -----------------------------------------------------------------
    # Inject rule-only player metadata derived from SSOT (DB).
    # - Do NOT read UI cache.
    # - Fail-fast if SSOT cannot provide metadata for any player in the deal.
    # -----------------------------------------------------------------
    deal_player_ids: list[str] = []
    seen: set[str] = set()
    # Avoid importing trades.models at module import time to reduce cycles.
    from ..models import PlayerAsset

    for _team_id, assets in getattr(deal, "legs", {}).items():
        for asset in assets or []:
            if not isinstance(asset, PlayerAsset):
                continue
            try:
                pid = _normalize_player_id(asset.player_id)
            except Exception as e:
                raise RuntimeError(f"Invalid player_id in deal asset: {asset!r}") from e
            if pid in seen:
                continue
            seen.add(pid)
            deal_player_ids.append(pid)

    players_meta = rule_player_meta.build_rule_players_meta(repo, deal_player_ids)
    missing = sorted(set(deal_player_ids) - set(players_meta.keys()))
    if missing:
        raise RuntimeError(
            "Trade rule evaluation requires SSOT-backed player meta; "
            f"missing meta for player_ids={missing}"
        )
    # Always present a players dict for rules; may be empty for pick-only deals.
    ctx_state["players"] = players_meta
    
    return TradeContext(
        game_state=ctx_state,
        repo=repo,
        db_path=resolved_db_path,
        current_date=current_date,
        extra=resolved_extra,
    )
