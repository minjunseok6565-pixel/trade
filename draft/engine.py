from __future__ import annotations

"""Draft engine orchestration.

This module ties together:
  - finalize: compute plan, settle picks in DB, build DraftTurn list
  - pool/session: in-memory draft session
  - ai/apply: autopick + persist drafted rookies to DB

MVP focus:
  - minimal end-to-end functionality
  - deterministic, reproducible
  - explicit dict shapes for UI / API integration
"""

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .finalize import finalize_draft_year, infer_db_path_from_state, infer_draft_year_from_state
from .types import DraftOrderPlan, DraftTurn, TeamId, norm_team_id
from .pool import DraftPool, Prospect, load_pool_from_db
from .session import DraftSession, DraftPick
from .ai import DraftAIPolicy, DraftAIContext, BPAByOVRPolicy
from .apply import apply_pick_to_db, RookieContractPolicy, SimpleRookieScalePolicy


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _infer_tx_date_from_state(state_snapshot: Mapping[str, Any]) -> str:
    league = state_snapshot.get("league", {}) if isinstance(state_snapshot, Mapping) else {}
    if isinstance(league, Mapping):
        cd = league.get("current_date")
        if cd:
            return str(cd)
    return _today_iso()


@dataclass(slots=True)
class DraftEngineBundle:
    """A fully prepared draft bundle (plan + turns + session + pool)."""

    draft_year: int
    db_path: str
    plan: DraftOrderPlan
    turns: List[DraftTurn]
    settlement_events: List[Dict[str, Any]] = field(default_factory=list)

    pool: DraftPool = None  # type: ignore[assignment]
    session: DraftSession = None  # type: ignore[assignment]

    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_year": int(self.draft_year),
            "db_path": str(self.db_path),
            "plan": self.plan.to_dict(),
            "turns": [t.to_dict() for t in self.turns],
            "settlement_events": list(self.settlement_events),
            "pool": None if self.pool is None else self.pool.to_dict(),
            "session": None if self.session is None else self.session.to_dict(),
            "meta": dict(self.meta) if isinstance(self.meta, dict) else {},
        }


def prepare_bundle_from_state(
    state_snapshot: Mapping[str, Any],
    *,
    rng_seed: int,
    tie_break_seed: Optional[int] = None,
    use_lottery: bool = True,
    settle_db: bool = True,
    db_path: Optional[str] = None,
    draft_year: Optional[int] = None,
    pool_limit: Optional[int] = None,
    pool_season_year: Optional[int] = None,
    session_meta: Optional[Dict[str, Any]] = None,
) -> DraftEngineBundle:
    """Compute order, settle picks, build turns, and create a session with a pool.

    Returns a DraftEngineBundle that you can run interactively or auto-complete.
    """
    dbp = str(db_path) if db_path is not None else infer_db_path_from_state(state_snapshot)
    dy = int(draft_year) if draft_year is not None else infer_draft_year_from_state(state_snapshot)

    finalized = finalize_draft_year(
        state_snapshot,
        db_path=dbp,
        draft_year=dy,
        rng_seed=int(rng_seed),
        tie_break_seed=tie_break_seed,
        use_lottery=bool(use_lottery),
        settle_db=bool(settle_db),
    )

    plan: DraftOrderPlan = finalized["plan"]
    turns: List[DraftTurn] = list(finalized["turns"])
    settlement_events = list(finalized.get("settlement_events") or [])

    # Pool + session
    pool = load_pool_from_db(
        db_path=dbp,
        draft_year=int(plan.draft_year),
        season_year=pool_season_year,
        limit=pool_limit,
    )
    session = DraftSession(
        draft_year=int(plan.draft_year),
        turns=turns,
        pool=pool,
        cursor=0,
        picks_by_turn_index={},
        meta=dict(session_meta or {}),
    )

    bundle = DraftEngineBundle(
        draft_year=int(plan.draft_year),
        db_path=dbp,
        plan=plan,
        turns=turns,
        settlement_events=settlement_events,
        pool=pool,
        session=session,
        meta={
            "rng_seed": int(rng_seed),
            "tie_break_seed": int(tie_break_seed) if tie_break_seed is not None else None,
            "pool_source": "college_db",
            "pool_limit": int(pool_limit) if pool_limit is not None else None,
            "pool_season_year": int(pool_season_year) if pool_season_year is not None else None,
            "use_lottery": bool(use_lottery),
            "settle_db": bool(settle_db),
        },
    )
    return bundle


def choose_ai_pick(
    *,
    policy: DraftAIPolicy,
    session: DraftSession,
) -> str:
    """Return prospect_temp_id chosen by AI for current turn."""
    turn = session.current_turn()
    ctx = DraftAIContext(draft_year=session.draft_year, team_id=turn.drafting_team, turn=turn, meta={})
    return str(policy.choose_prospect_temp_id(session.pool, ctx))


def apply_pick_and_record(
    *,
    bundle: DraftEngineBundle,
    prospect_temp_id: str,
    contract_policy: Optional[RookieContractPolicy] = None,
    contract_years: int = 4,
    tx_date_iso: Optional[str] = None,
    source: str = "draft",
) -> DraftPick:
    """Reserve+apply a pick and ensure the session state stays consistent.

    Flow (safe for apply failures):
      1) session.record_pick() with no DB ids yet (marks pool + advances cursor)
      2) apply_pick_to_db() (DB writes)
      3) replace the stored DraftPick in session.picks_by_turn_index with IDs
      4) if apply fails, rollback in-memory state (pool + cursor + picks map)
    """
    session = bundle.session
    if session is None:
        raise RuntimeError("bundle.session is None")

    turn_index = int(session.cursor)
    turn = session.current_turn()
    tid = str(prospect_temp_id)

    # 1) reserve in session
    dp0 = session.record_pick(prospect_temp_id=tid, player_id=None, contract_id=None, meta={"reserved": True})

    # 2) apply in DB
    try:
        prospect = session.pool.get(tid)
        tx_date = tx_date_iso or _today_iso()
        result = apply_pick_to_db(
            db_path=bundle.db_path,
            turn=turn,
            prospect=prospect,
            draft_year=int(bundle.draft_year),
            contract_policy=contract_policy or SimpleRookieScalePolicy(),
            contract_years=int(contract_years),
            tx_date_iso=str(tx_date),
            source=str(source),
        )
    except Exception as exc:
        # rollback in-memory
        session.cursor = int(turn_index)
        session.picks_by_turn_index.pop(int(turn_index), None)
        session.pool.unmark_picked(tid)
        raise

    # 3) replace pick record with DB ids
    dp = DraftPick(
        overall_no=dp0.overall_no,
        round=dp0.round,
        slot=dp0.slot,
        pick_id=dp0.pick_id,
        drafting_team=dp0.drafting_team,
        prospect_temp_id=dp0.prospect_temp_id,
        player_id=result.player_id,
        contract_id=result.contract_id,
        meta=dict(dp0.meta or {}) | {"reserved": False, "tx": result.tx_entry},
    )
    session.picks_by_turn_index[int(turn_index)] = dp
    return dp


def auto_run_draft(
    *,
    bundle: DraftEngineBundle,
    policy: Optional[DraftAIPolicy] = None,
    contract_policy: Optional[RookieContractPolicy] = None,
    contract_years: int = 4,
    tx_date_iso: Optional[str] = None,
    max_picks: Optional[int] = None,
    source: str = "draft_ai",
) -> List[DraftPick]:
    """Auto-complete the draft (or up to max_picks)."""
    sess = bundle.session
    if sess is None:
        raise RuntimeError("bundle.session is None")
    pol = policy or BPAByOVRPolicy()

    picks: List[DraftPick] = []
    limit = int(max_picks) if max_picks is not None else None

    while not sess.is_complete():
        if limit is not None and len(picks) >= limit:
            break
        tid = choose_ai_pick(policy=pol, session=sess)
        dp = apply_pick_and_record(
            bundle=bundle,
            prospect_temp_id=tid,
            contract_policy=contract_policy,
            contract_years=int(contract_years),
            tx_date_iso=tx_date_iso,
            source=str(source),
        )
        picks.append(dp)

    return picks


# Convenience wrappers that operate on global state (optional).
def prepare_bundle_from_global_state(
    *,
    rng_seed: int,
    tie_break_seed: Optional[int] = None,
    use_lottery: bool = True,
    settle_db: bool = True,
    pool_limit: Optional[int] = None,
    pool_season_year: Optional[int] = None,
    session_meta: Optional[Dict[str, Any]] = None,
) -> DraftEngineBundle:
    import state  # local import to avoid cycles at module import time

    snap = state.export_full_state_snapshot()
    return prepare_bundle_from_state(
        snap,
        rng_seed=int(rng_seed),
        tie_break_seed=tie_break_seed,
        use_lottery=bool(use_lottery),
        settle_db=bool(settle_db),
        db_path=None,
        draft_year=None,
        pool_limit=pool_limit,
        pool_season_year=pool_season_year,
        session_meta=session_meta,
    )
