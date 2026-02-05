from __future__ import annotations

"""
trades/valuation/service.py

IO / orchestration layer that wires the *pure* valuation engine into the project.

Ownership boundaries (enforced by design)
-----------------------------------------
This module MAY:
- call validate_deal(...) to ensure legality/feasibility (salary matching, Stepien, apron rules, locks, etc.)
- build TeamSituation and DecisionContext (team_situation.py + decision_context.py)
- build ValuationDataProvider backed by LeagueRepo snapshots (data_context.py)
- call valuation engine: deal_evaluator + decision_policy

This module MUST NOT:
- re-implement rule validation or re-check hard constraints (that's validator + trades/rules/*)
- re-interpret team status or re-generate needs (that's team_situation.py)
- create new "knobs" or override DecisionContext outputs (that's decision_context.py)
"""

from dataclasses import replace
from datetime import date
from typing import Any, Dict, Optional, Sequence, Tuple

import random

# --- Project-level trade types / errors ---
from ..models import Deal
from ..errors import TradeError
from ..validator import validate_deal

# --- Valuation engine (pure) ---
from .deal_evaluator import evaluate_deal_for_team as _evaluate_deal_for_team
from .decision_policy import decide_deal as _decide_deal
from .types import DealDecision, TeamDealEvaluation, TeamSideValuation

# --- Valuation data provider (Repo IO layer) ---
from .data_context import (
    RepoValuationDataContext,
    PickExpectationMap,
    build_repo_valuation_data_context,
)

# --- team_situation / decision_context live outside trades/ in the current project layout.
# Keep imports flexible so the same file survives refactors.
try:
    from team_situation import build_team_situation_context, TeamSituationEvaluator  # type: ignore
except Exception:  # pragma: no cover
    from data.team_situation import build_team_situation_context, TeamSituationEvaluator  # type: ignore

try:
    from decision_context import (  # type: ignore
        DecisionContext,
        GMTradeTraits,
        build_decision_context,
        gm_traits_from_profile_json,
    )
except Exception:  # pragma: no cover
    from data.decision_context import (  # type: ignore
        DecisionContext,
        GMTradeTraits,
        build_decision_context,
        gm_traits_from_profile_json,
    )

try:
    import state  # type: ignore
except Exception as exc:  # pragma: no cover
    state = None  # type: ignore

try:
    from league_repo import LeagueRepo  # type: ignore
except Exception as exc:  # pragma: no cover
    LeagueRepo = None  # type: ignore

try:
    from schema import normalize_team_id  # type: ignore
except Exception:  # pragma: no cover
    def normalize_team_id(x: str, strict: bool = False) -> str:  # type: ignore
        return str(x or "").upper()


# -----------------------------------------------------------------------------
# Small helpers (defensive; service layer should not crash the server on minor gaps)
# -----------------------------------------------------------------------------
def _safe_date(d: Any) -> date:
    if isinstance(d, date):
        return d
    # accept ISO string
    if isinstance(d, str):
        try:
            return date.fromisoformat(d)
        except Exception:
            pass
    # fall back to in-game date if state exists, else today
    if state is not None:
        try:
            return state.get_current_date_as_date()
        except Exception:
            pass
    return date.today()


def _safe_db_path(db_path: Optional[str]) -> str:
    if db_path:
        return str(db_path)
    if state is not None:
        try:
            return str(state.get_db_path())
        except Exception:
            pass
    raise TradeError(code="MISSING_DB_PATH", message="db_path is required for valuation service")


def _resolve_current_season_year(current_season_year: Optional[int], *, current_date: date) -> int:
    if current_season_year is not None:
        try:
            return int(current_season_year)
        except Exception:
            pass
    # state.league.season_year is the SSOT in this project
    if state is not None:
        try:
            snap = state.snapshot_state()
            league = snap.get("league") if isinstance(snap, dict) else None
            sy = (league or {}).get("season_year") if isinstance(league, dict) else None
            if sy:
                return int(sy)
        except Exception:
            pass
    # fallback: current year (good enough for debug mode; can be tightened later)
    return int(getattr(current_date, "year", 0) or date.today().year)


def _build_standings_order_worst_to_best(team_situation_ctx: Any) -> Optional[Sequence[str]]:
    """
    Build a league-wide standings order worst->best for pick expectation heuristics.
    Uses only already-snapshotted data (no new DB/state reads).
    """
    rec_index = getattr(team_situation_ctx, "records_index", None)
    if not isinstance(rec_index, dict) or not rec_index:
        return None

    rows = []
    for tid, rec in rec_index.items():
        if not isinstance(rec, dict):
            continue
        wins = int(rec.get("wins", 0) or 0)
        losses = int(rec.get("losses", 0) or 0)
        gp = wins + losses
        win_pct = (wins / gp) if gp > 0 else 0.0
        pf = float(rec.get("pf", 0) or 0.0)
        pa = float(rec.get("pa", 0) or 0.0)
        point_diff_pg = ((pf - pa) / gp) if gp > 0 else 0.0
        rows.append((str(tid).upper(), float(win_pct), float(point_diff_pg)))

    # worst -> best: lowest win_pct, then lowest point_diff_pg
    rows_sorted = sorted(rows, key=lambda x: (x[1], x[2], x[0]))
    return [r[0] for r in rows_sorted]


def _strip_breakdown(side: TeamSideValuation, evaluation: TeamDealEvaluation) -> Tuple[TeamSideValuation, TeamDealEvaluation]:
    """
    Remove step-by-step breakdown tuples to reduce payload size when requested.
    (Keeps numeric totals and high-level fit flags.)
    """
    def _strip_tv(tv):
        return replace(tv, market_steps=tuple(), team_steps=tuple())

    incoming = tuple(_strip_tv(tv) for tv in side.incoming)
    outgoing = tuple(_strip_tv(tv) for tv in side.outgoing)
    side2 = replace(side, incoming=incoming, outgoing=outgoing, package_steps=tuple())
    eval2 = replace(evaluation, side=side2)
    return side2, eval2


# -----------------------------------------------------------------------------
# Public API (service entrypoint)
# -----------------------------------------------------------------------------
def evaluate_deal_for_team(
    deal: Deal,
    team_id: str,
    *,
    current_date: Optional[date] = None,
    db_path: Optional[str] = None,
    current_season_year: Optional[int] = None,
    standings_order_worst_to_best: Optional[Sequence[str]] = None,
    pick_expectations: Optional[PickExpectationMap] = None,
    include_breakdown: bool = True,
    include_package_effects: bool = True,
    allow_counter: bool = True,
    rng: Optional[random.Random] = None,
    rng_seed: Optional[int] = None,
    allow_locked_by_deal_id: Optional[str] = None,
    validate: bool = True,
) -> Tuple[DealDecision, TeamDealEvaluation]:
    """
    Evaluate a deal from `team_id`'s perspective and return (decision, evaluation).

    This is the canonical orchestration function that the debug API should call.

    Parameters
    ----------
    deal:
        A canonical Deal object (see trades.models).
    team_id:
        Evaluating team id.
    current_date:
        In-game date. Defaults to state.get_current_date_as_date() when available.
    db_path:
        SQLite DB path. Defaults to state.get_db_path() when available.
    current_season_year:
        Season year for contract/pick calculations. Defaults to state.league.season_year when available.
    standings_order_worst_to_best:
        Optional league-wide order used for pick expectation heuristic.
        If not provided, we attempt to derive from team_situation snapshot records_index.
    pick_expectations:
        If provided, overrides standings-based expectation builder.
    include_breakdown:
        If False, strips verbose step logs from the returned evaluation (lighter payload).
    include_package_effects:
        Apply package effects (diminishing returns / roster saturation, etc.)
    allow_counter:
        Whether the decision policy is allowed to return COUNTER.
    rng / rng_seed:
        Control the (optional) stochastic edge behavior for counter decisions.
    allow_locked_by_deal_id:
        Pass-through to validate_deal for committed-deal lock exceptions.
    validate:
        If True, runs validate_deal first. (Recommended for server usage.)
    """
    tid = normalize_team_id(team_id, strict=False)

    cd = _safe_date(current_date)
    dbp = _safe_db_path(db_path)
    season_year = _resolve_current_season_year(current_season_year, current_date=cd)

    # RNG setup: default deterministic if no rng supplied.
    if rng is None:
        rng = random.Random(rng_seed) if rng_seed is not None else random.Random(0)

    # 1) Hard rule validation (salary matching, Stepien, apron, locks, etc.)
    if validate:
        validate_deal(
            deal,
            current_date=cd,
            allow_locked_by_deal_id=allow_locked_by_deal_id,
            db_path=dbp,
        )

    # 2) TeamSituation snapshot + per-team evaluation
    ts_ctx = build_team_situation_context(db_path=dbp, current_date=cd)
    ts_eval = TeamSituationEvaluator(ctx=ts_ctx, db_path=dbp).evaluate_team(tid)

    # 3) GM profile -> GMTradeTraits -> DecisionContext
    gm_profile: Dict[str, Any] = {}
    if LeagueRepo is None:  # pragma: no cover
        raise TradeError(code="LEAGUE_REPO_IMPORT_FAILED", message="LeagueRepo import failed; cannot read gm profile")

    try:
        with LeagueRepo(dbp) as repo:
            gp = repo.get_gm_profile(tid) or {}
            gm_profile = dict(gp) if isinstance(gp, dict) else {"value": gp}
    except Exception as exc:
        # Fallback to default mid traits when profile missing or read fails.
        gm_profile = {}

    gm_traits: GMTradeTraits = gm_traits_from_profile_json(gm_profile, default=GMTradeTraits())
    ctx: DecisionContext = build_decision_context(team_situation=ts_eval, gm_traits=gm_traits, team_id=tid)

    # 4) Build valuation provider (trade assets + contract ledger + pick expectations)
    order = standings_order_worst_to_best or _build_standings_order_worst_to_best(ts_ctx)
    provider: RepoValuationDataContext = build_repo_valuation_data_context(
        db_path=dbp,
        current_season_year=season_year,
        current_date_iso=cd.isoformat(),
        standings_order_worst_to_best=order,
        pick_expectations=pick_expectations,
    )

    # 5) Pure valuation (market -> team utility -> package effects)
    side, evaluation = _evaluate_deal_for_team(
        deal=deal,
        team_id=tid,
        ctx=ctx,
        provider=provider,
        include_package_effects=include_package_effects,
        attach_leg_metadata=True,
    )

    # 6) Decision
    decision = _decide_deal(
        evaluation=evaluation,
        ctx=ctx,
        rng=rng,
        allow_counter=allow_counter,
    )

    # 7) Optional payload slimming
    if not include_breakdown:
        side2, evaluation2 = _strip_breakdown(side, evaluation)
        # Keep decision as-is (it is already small).
        return decision, evaluation2

    return decision, evaluation
