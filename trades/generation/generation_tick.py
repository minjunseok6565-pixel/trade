from __future__ import annotations

"""generation_tick.py

Tick-scoped context for trade deal generation.

Why this exists
---------------
A deal generator typically explores *many* candidate deals per tick. Without
caching, each candidate ends up re-building expensive snapshots and contexts
(rule validation context, team situation evaluation inputs, GM traits/decision
knobs, valuation provider snapshots).

TradeGenerationTickContext bundles these into a single object constructed once
per tick, so the generator can validate/evaluate/prune candidates cheaply.

Notes
-----
This file is intentionally designed to be compatible with the current codebase
*and* forward-compatible with the follow-up refactors (repo sharing in
TeamSituationEvaluator / RepoValuationDataContext). It uses feature-detection
(via inspect.signature) to pass optional parameters only when supported.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Mapping, Optional, Sequence

import inspect

from league_repo import LeagueRepo

# --- Project-level modules live outside trades/ in the current project layout.
# Keep imports flexible so this file survives future package refactors.
try:
    from config import ALL_TEAM_IDS  # type: ignore
except Exception:  # pragma: no cover
    from trade.config import ALL_TEAM_IDS  # type: ignore

try:
    from decision_context import (  # type: ignore
        DecisionContext,
        GMTradeTraits,
        build_decision_context,
        gm_traits_from_profile_json,
    )
except Exception:  # pragma: no cover
    from trade.decision_context import (  # type: ignore
        DecisionContext,
        GMTradeTraits,
        build_decision_context,
        gm_traits_from_profile_json,
    )

# team_situation historically lived at data/team_situation.py in this project.
try:
    from team_situation import (  # type: ignore
        TeamSituation,
        TeamSituationContext,
        TeamSituationEvaluator,
        build_team_situation_context,
    )
except Exception:  # pragma: no cover
    from data.team_situation import (  # type: ignore
        TeamSituation,
        TeamSituationContext,
        TeamSituationEvaluator,
        build_team_situation_context,
    )
from ..models import Deal
from ..validator import validate_deal as _validate_deal
from ..rules.tick_context import TradeRuleTickContext, build_trade_rule_tick_context
from ..valuation.data_context import (
    PickExpectationMap,
    RepoValuationDataContext,
    build_repo_valuation_data_context,
)
from .asset_catalog import TradeAssetCatalog, build_trade_asset_catalog


def _canon_team_id(team_id: Any) -> str:
    raw = str(team_id or "").strip()
    if not raw:
        return ""
    try:
        from schema import normalize_team_id  # type: ignore

        return str(normalize_team_id(raw, strict=False)).strip()
    except Exception:
        return raw.upper()


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return int(default)
        return int(x)
    except Exception:
        return int(default)


def _compute_standings_order_worst_to_best(
    ts_ctx: TeamSituationContext,
    team_ids: Sequence[str],
) -> list[str]:
    """Compute a worst->best ordering using records_index (wins/losses + point diff).

    This is a lightweight heuristic intended for pick expectation estimation.
    """

    rows = []
    for tid in team_ids:
        t = _canon_team_id(tid)
        rec = ts_ctx.records_index.get(t, {}) or {}
        wins = _safe_int(rec.get("wins"), 0)
        losses = _safe_int(rec.get("losses"), 0)
        gp = wins + losses
        win_pct = (wins / gp) if gp > 0 else 0.0
        pf = float(rec.get("pf", 0) or 0.0)
        pa = float(rec.get("pa", 0) or 0.0)
        point_diff = (pf - pa) / gp if gp > 0 else (pf - pa)
        rows.append((win_pct, point_diff, t))

    # Worst first: lower win% is worse; tie-break lower point diff is worse.
    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    return [t for _, __, t in rows if t]


def _build_provider(
    *,
    db_path: str,
    season_year: int,
    current_date: date,
    standings_order_worst_to_best: Optional[Sequence[str]],
    pick_expectations: Optional[PickExpectationMap],
    repo: LeagueRepo,
    ts_ctx: TeamSituationContext,
) -> RepoValuationDataContext:
    """Build valuation provider with best-effort snapshot reuse.

    Current codebase may not support repo/assets_snapshot injection yet.
    We only pass those args when the builder supports them.
    """

    kwargs: Dict[str, Any] = {
        "db_path": str(db_path),
        "current_season_year": int(season_year),
        "current_date_iso": str(current_date.isoformat()),
        "standings_order_worst_to_best": list(standings_order_worst_to_best) if standings_order_worst_to_best else None,
        "pick_expectations": dict(pick_expectations) if pick_expectations is not None else None,
    }

    sig = inspect.signature(build_repo_valuation_data_context)
    if "repo" in sig.parameters:
        kwargs["repo"] = repo
    if "assets_snapshot" in sig.parameters:
        kwargs["assets_snapshot"] = dict(getattr(ts_ctx, "assets_snapshot", {}) or {})
    if "contract_ledger" in sig.parameters:
        kwargs["contract_ledger"] = dict(getattr(ts_ctx, "contract_ledger", {}) or {})

    # Filter None keys that some builders might not accept semantically
    # (but still accept as parameter). Keep it minimal.
    return build_repo_valuation_data_context(**kwargs)  # type: ignore[arg-type]


@dataclass(slots=True)
class TradeGenerationTickContext:
    """Tick-level cache bundle for deal generation."""

    db_path: str
    current_date: date
    season_year: int

    repo: LeagueRepo
    owns_repo: bool

    # fast hard-rule validation
    rule_tick_ctx: TradeRuleTickContext

    # league snapshot for situation evaluation
    team_situation_ctx: TeamSituationContext

    # caches
    team_situations: Dict[str, TeamSituation]
    gm_profiles: Dict[str, Dict[str, Any]]
    gm_traits: Dict[str, GMTradeTraits]
    decision_contexts: Dict[str, DecisionContext]

    # valuation provider (snapshots + caches 유지)
    provider: RepoValuationDataContext

    # tradable asset catalog (outgoing buckets / incoming indices / picks/swaps)
    asset_catalog: Optional[TradeAssetCatalog] = None

    standings_order_worst_to_best: Optional[Sequence[str]] = None

    def get_team_situation(self, team_id: str) -> TeamSituation:
        tid = _canon_team_id(team_id)
        if tid in self.team_situations:
            return self.team_situations[tid]
        # Fallback: evaluate on-demand (should be rare)
        evaluator_kwargs: Dict[str, Any] = {"ctx": self.team_situation_ctx, "db_path": self.db_path}
        try:
            init_sig = inspect.signature(TeamSituationEvaluator.__init__)
            if "repo" in init_sig.parameters:
                evaluator_kwargs["repo"] = self.repo
        except Exception:
            pass
        evaluator = TeamSituationEvaluator(**evaluator_kwargs)  # type: ignore[arg-type]
        ts = evaluator.evaluate_team(tid)
        self.team_situations[tid] = ts
        return ts

    def get_decision_context(self, team_id: str) -> DecisionContext:
        tid = _canon_team_id(team_id)
        if tid in self.decision_contexts:
            return self.decision_contexts[tid]
        ts = self.get_team_situation(tid)
        traits = self.gm_traits.get(tid, GMTradeTraits())
        dc = build_decision_context(team_situation=ts, gm_traits=traits, team_id=tid)
        self.decision_contexts[tid] = dc
        return dc

    def validate_deal(
        self,
        deal: Deal,
        *,
        allow_locked_by_deal_id: Optional[str] = None,
        integrity_check: Optional[bool] = None,
    ) -> None:
        """Hard-rule validation using tick-level caches."""
        _validate_deal(
            deal,
            current_date=self.current_date,
            allow_locked_by_deal_id=allow_locked_by_deal_id,
            db_path=self.db_path,
            tick_ctx=self.rule_tick_ctx,
            integrity_check=integrity_check,
        )

    def close(self) -> None:
        # Close rule context first (it will not close shared repo if owns_repo=False)
        try:
            self.rule_tick_ctx.close()
        except Exception:
            pass
        # Close repo if owned
        try:
            if self.owns_repo:
                self.repo.close()
        except Exception:
            pass

    def __enter__(self) -> "TradeGenerationTickContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def build_trade_generation_tick_context(
    *,
    current_date: Optional[date] = None,
    db_path: Optional[str] = None,
    validate_integrity: bool = True,
    team_ids: Optional[Sequence[str]] = None,
    pick_expectations: Optional[PickExpectationMap] = None,
    standings_order_worst_to_best: Optional[Sequence[str]] = None,
) -> TradeGenerationTickContext:
    """Build a tick-scoped generation context.

    Parameters
    ----------
    current_date:
        Defaults to state.get_current_date_as_date().
    db_path:
        Defaults to state.get_db_path().
    validate_integrity:
        If True, validates repo integrity once during build.
    team_ids:
        Subset of teams to cache. Defaults to ALL_TEAM_IDS.
    pick_expectations / standings_order_worst_to_best:
        Optional overrides for valuation provider expectations.
    """

    import state
    from ..maintenance import maintain_trade_state

    resolved_current_date = current_date or state.get_current_date_as_date()
    resolved_db_path = str(db_path or state.get_db_path())

    # Tick maintenance should only run when we're operating on the SSOT's current date.
    # This prevents test/simulation calls with hypothetical dates from mutating live state.
    ssot_date = state.get_current_date_as_date()
    if current_date is None or resolved_current_date == ssot_date:
        maintain_trade_state(current_date=resolved_current_date, db_path=resolved_db_path)

    # Shared repo for the tick.
    repo = LeagueRepo(resolved_db_path)
    owns_repo = True
    rule_tick_ctx: Optional[TradeRuleTickContext] = None

    try:
        # Build rule tick ctx using shared repo.
        rule_tick_ctx = build_trade_rule_tick_context(
            current_date=resolved_current_date,
            db_path=resolved_db_path,
            validate_integrity=validate_integrity,
            repo=repo,
        )

        # Build team situation context with best-effort snapshot reuse.
        team_situation_ctx = build_team_situation_context(
            db_path=resolved_db_path,
            current_date=resolved_current_date,
            repo=repo,
            trade_state_snapshot=getattr(rule_tick_ctx, "ctx_state_base", None),
            assets_snapshot=getattr(rule_tick_ctx, "assets_snapshot", None),
            contract_ledger=None,
        )

        # Determine which teams to precompute.
        ids_raw = list(team_ids) if team_ids is not None else list(ALL_TEAM_IDS)
        ids = [_canon_team_id(t) for t in ids_raw if _canon_team_id(t)]

        # Evaluate TeamSituation for all teams.
        # (Forward compatible: pass repo if evaluator supports it.)
        evaluator_kwargs: Dict[str, Any] = {"ctx": team_situation_ctx, "db_path": resolved_db_path}
        try:
            init_sig = inspect.signature(TeamSituationEvaluator.__init__)
            if "repo" in init_sig.parameters:
                evaluator_kwargs["repo"] = repo
        except Exception:
            pass

        evaluator = TeamSituationEvaluator(**evaluator_kwargs)  # type: ignore[arg-type]
        team_situations = evaluator.evaluate_all(list(ids))

        # GM profiles snapshot (from trade context snapshot).
        gm_profiles_raw = {}
        try:
            gm_profiles_raw = (getattr(rule_tick_ctx, "ctx_state_base", {}) or {}).get("teams") or {}
        except Exception:
            gm_profiles_raw = {}

        gm_profiles: Dict[str, Dict[str, Any]] = {}
        if isinstance(gm_profiles_raw, Mapping):
            for k, v in gm_profiles_raw.items():
                tid = _canon_team_id(k)
                if not tid:
                    continue
                if isinstance(v, Mapping):
                    gm_profiles[tid] = dict(v)
                else:
                    gm_profiles[tid] = {"value": v}

        # Build DecisionContext per team.
        gm_traits: Dict[str, GMTradeTraits] = {}
        decision_contexts: Dict[str, DecisionContext] = {}
        for tid in ids:
            ts = team_situations.get(tid)
            if ts is None:
                continue
            profile = gm_profiles.get(tid, {})
            traits = gm_traits_from_profile_json(profile, default=GMTradeTraits())
            gm_traits[tid] = traits
            decision_contexts[tid] = build_decision_context(team_situation=ts, gm_traits=traits, team_id=tid)

        # Compute standings order if not provided.
        standings = list(standings_order_worst_to_best) if standings_order_worst_to_best is not None else None
        if standings is None:
            try:
                standings = _compute_standings_order_worst_to_best(team_situation_ctx, ids)
            except Exception:
                standings = None

        provider = _build_provider(
            db_path=resolved_db_path,
            season_year=int(getattr(rule_tick_ctx, "season_year", 0) or 0),
            current_date=resolved_current_date,
            standings_order_worst_to_best=standings,
            pick_expectations=pick_expectations,
            repo=repo,
            ts_ctx=team_situation_ctx,
        )

        season_year = int(getattr(rule_tick_ctx, "season_year", 0) or 0)

        tick = TradeGenerationTickContext(
            db_path=resolved_db_path,
            current_date=resolved_current_date,
            season_year=season_year,
            repo=repo,
            owns_repo=owns_repo,
            rule_tick_ctx=rule_tick_ctx,
            team_situation_ctx=team_situation_ctx,
            team_situations=dict(team_situations),
            gm_profiles=gm_profiles,
            gm_traits=gm_traits,
            decision_contexts=decision_contexts,
            provider=provider,
            standings_order_worst_to_best=standings,
        )

        # Build tradable asset catalog once per tick.
        # This is intentionally constructed after the tick object so the catalog
        # builder can reuse its caches (rule_tick_ctx / provider / team situations).
        tick.asset_catalog = build_trade_asset_catalog(tick_ctx=tick)
        return tick


    except Exception:
        # On build failure, close the rule context (if created) and the shared repo.
        try:
            if rule_tick_ctx is not None:
                rule_tick_ctx.close()
        except Exception:
            pass
        try:
            repo.close()
        except Exception:
            pass
        raise
