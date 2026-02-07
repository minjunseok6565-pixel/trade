from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional, Protocol, TYPE_CHECKING

from league_repo import LeagueRepo
from schema import normalize_player_id, normalize_team_id


if TYPE_CHECKING:
    from .tick_context import TradeRuleTickContext


@dataclass
class TradeContext:
    game_state: dict
    repo: LeagueRepo
    db_path: Optional[str]
    current_date: date
    extra: dict[str, Any] = field(default_factory=dict)
    owns_repo: bool = True
    tick_ctx: Optional["TradeRuleTickContext"] = None

    # -----------------------------
    # Fast accessors (prefer tick caches when available)
    # -----------------------------
    def _ensure_active_roster_index(self) -> None:
        if self.tick_ctx is not None:
            self.tick_ctx.ensure_active_roster_index()

    def get_salary_amount(self, player_id: Any) -> Optional[int]:
        pid = _normalize_player_id(player_id)
        if self.tick_ctx is not None:
            self._ensure_active_roster_index()
            if pid in self.tick_ctx.player_salary_map:
                return self.tick_ctx.player_salary_map.get(pid)
        return self.repo.get_salary_amount(pid)

    def get_team_id_by_player(self, player_id: Any) -> str:
        pid = _normalize_player_id(player_id)
        if self.tick_ctx is not None:
            self._ensure_active_roster_index()
            team = self.tick_ctx.player_team_map.get(pid)
            if team is not None:
                return str(team)
        return self.repo.get_team_id_by_player(pid)

    def get_roster_player_ids(self, team_id: Any) -> set[str]:
        tid = _normalize_team_id(team_id)
        if self.tick_ctx is not None:
            self._ensure_active_roster_index()
            ids = self.tick_ctx.team_roster_ids_map.get(tid)
            if ids is not None:
                return ids
        return self.repo.get_roster_player_ids(tid)

    def get_team_payroll_before(self, team_id: Any) -> float:
        tid = _normalize_team_id(team_id)
        if self.tick_ctx is not None:
            self._ensure_active_roster_index()
            if tid in self.tick_ctx.team_payroll_before_map:
                return float(self.tick_ctx.team_payroll_before_map.get(tid) or 0.0)
        # Fallback: compute from roster join (slower, but correct).
        return float(sum(float(row.get("salary_amount") or 0) for row in self.repo.get_team_roster(tid)))


class Rule(Protocol):
    rule_id: str
    priority: int
    enabled: bool

    def validate(self, deal: Any, ctx: TradeContext) -> None:
        ...


def build_player_moves(deal: Any) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    # SSOT for receiver resolution lives in trades.models.resolve_asset_receiver
    # (Do not duplicate receiver inference logic in rules.)
    from ..models import PlayerAsset, resolve_asset_receiver

    players_out: dict[str, list[str]] = {team_id: [] for team_id in deal.teams}
    players_in: dict[str, list[str]] = {team_id: [] for team_id in deal.teams}

    for team_id, assets in deal.legs.items():
        for asset in assets:
            if not isinstance(asset, PlayerAsset):
                continue
            player_id = _normalize_player_id(asset.player_id)
            players_out[team_id].append(player_id)
            receiver = resolve_asset_receiver(deal, team_id, asset)
            players_in[receiver].append(player_id)

    return players_out, players_in


def _normalize_player_id(value: Any) -> str:
    return str(normalize_player_id(value, strict=False, allow_legacy_numeric=True))


def _normalize_team_id(value: Any) -> str:
    return str(normalize_team_id(value, strict=True))


def _sum_player_salaries(ctx: TradeContext, player_ids: list[str]) -> float:
    if not player_ids:
        return 0.0
    total = 0.0
    for player_id in player_ids:
        pid = _normalize_player_id(player_id)
        salary = ctx.get_salary_amount(pid)
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
            "outgoing_salary": _sum_player_salaries(ctx, outgoing_players),
            "incoming_salary": _sum_player_salaries(ctx, incoming_players),
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
        payroll_before = float(ctx.get_team_payroll_before(tid))
        outgoing_salary = float(totals[team_id]["outgoing_salary"])
        incoming_salary = float(totals[team_id]["incoming_salary"])
        payrolls[team_id] = {
            "payroll_before": payroll_before,
            "payroll_after": payroll_before - outgoing_salary + incoming_salary,
        }

    return payrolls


def build_trade_context(
    deal: Any,
    current_date: Optional[date] = None,
    extra: Optional[dict[str, Any]] = None,
    db_path: Optional[str] = None,
    tick_ctx: Optional["TradeRuleTickContext"] = None,
) -> TradeContext:
    import state
    from . import rule_player_meta

    if tick_ctx is not None:
        if current_date is None:
            current_date = tick_ctx.current_date
        elif current_date != tick_ctx.current_date:
            raise ValueError("build_trade_context: current_date conflicts with tick_ctx.current_date")

    if current_date is None:
        current_date = state.get_current_date_as_date()

    resolved_extra = dict(extra) if extra else {}

    if tick_ctx is not None:
        resolved_db_path = tick_ctx.db_path
        if db_path is not None and str(db_path) != str(resolved_db_path):
            raise ValueError("build_trade_context: db_path conflicts with tick_ctx.db_path")
        repo = tick_ctx.repo
        owns_repo = False
    else:
        resolved_db_path = db_path or state.get_db_path()
        repo = LeagueRepo(resolved_db_path)
        owns_repo = True
    # DB schema is guaranteed during server startup (state.startup_init_state()).

    if tick_ctx is not None:
        ctx_state_base = tick_ctx.ctx_state_base
        assets_snap = tick_ctx.assets_snapshot
        season_year = tick_ctx.season_year
    else:
        ctx_state_base = state.export_trade_context_snapshot(db_path=resolved_db_path, repo=repo)
        assets_snap = state.export_trade_assets_snapshot(db_path=resolved_db_path, repo=repo)
        season_year = int(ctx_state_base["league"]["season_year"])
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

    # Fail-fast if season_year is missing or current_date format is inconsistent.
    # season_year SSOT is ctx_state["league"]["season_year"] (from league context snapshot).
    if tick_ctx is not None:
        players_meta = tick_ctx.ensure_players_meta(deal_player_ids)
    else:
        players_meta = rule_player_meta.build_rule_players_meta(
            repo, deal_player_ids, season_year=season_year, as_of_date=current_date
        )
    missing = sorted(set(deal_player_ids) - set(players_meta.keys()))
    if missing:
        raise RuntimeError(
            "Trade rule evaluation requires SSOT-backed player meta; "
            f"missing meta for player_ids={missing}"
        )
        
    # -----------------------------------------------------------------
    # Per-deal rule context MUST NOT mutate tick-level snapshot state.
    # Copy known-mutable submaps so rule evaluation stays order-independent.
    # -----------------------------------------------------------------
    ctx_state = dict(ctx_state_base)
    try:
        asset_locks = ctx_state.get("asset_locks")
        if isinstance(asset_locks, dict):
            ctx_state["asset_locks"] = dict(asset_locks)
        elif asset_locks is None:
            ctx_state["asset_locks"] = {}
    except Exception:
        ctx_state["asset_locks"] = {}
    ctx_state["players"] = players_meta
    
    return TradeContext(
        game_state=ctx_state,
        repo=repo,
        db_path=resolved_db_path,
        current_date=current_date,
        extra=resolved_extra,
        owns_repo=owns_repo,
        tick_ctx=tick_ctx,
    )
