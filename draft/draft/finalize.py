from __future__ import annotations

"""Draft finalization (DB-integrated).

This module bridges:
  - (pure) standings/lottery/order computations
  - (DB) pick settlement (protections + swap rights) via LeagueService.settle_draft_year
  - (DB) resolving drafting team (pick owner_team after settlement)
  - turning the plan into a turn list (DraftTurn)

Output is still *ephemeral* (in-memory): turns, plan, and settlement events can be stored
in UI/cache if desired, but do not need to be persisted as an authoritative SSOT.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config import ALL_TEAM_IDS

from .types import DraftOrderPlan, DraftTurn, TeamId, make_pick_id, norm_team_id
from .standings import compute_team_records_from_master_schedule
from .order import compute_draft_order_plan_from_records


def infer_draft_year_from_state(state_snapshot: Mapping[str, Any]) -> int:
    league = state_snapshot.get("league", {}) if isinstance(state_snapshot, Mapping) else {}
    if isinstance(league, Mapping):
        dy = league.get("draft_year")
        if dy is not None and str(dy) != "":
            try:
                return int(dy)
            except (TypeError, ValueError):
                pass
        sy = league.get("season_year")
        if sy is not None and str(sy) != "":
            try:
                return int(sy) + 1
            except (TypeError, ValueError):
                pass
    raise ValueError("Cannot infer draft_year from state_snapshot (expected league.draft_year or league.season_year)")


def infer_db_path_from_state(state_snapshot: Mapping[str, Any]) -> str:
    league = state_snapshot.get("league", {}) if isinstance(state_snapshot, Mapping) else {}
    if isinstance(league, Mapping):
        db_path = league.get("db_path")
        if db_path:
            return str(db_path)
    raise ValueError("Cannot infer db_path from state_snapshot (expected league.db_path)")


def compute_plan_from_state(
    state_snapshot: Mapping[str, Any],
    *,
    draft_year: Optional[int] = None,
    rng_seed: int,
    tie_break_seed: Optional[int] = None,
    use_lottery: bool = True,
) -> DraftOrderPlan:
    dy = int(draft_year) if draft_year is not None else infer_draft_year_from_state(state_snapshot)

    records = compute_team_records_from_master_schedule(
        state_snapshot,
        team_ids=list(ALL_TEAM_IDS),
        require_initialized_schedule=True,
    )

    plan = compute_draft_order_plan_from_records(
        draft_year=dy,
        records=records,
        rng_seed=int(rng_seed),
        tie_break_seed=tie_break_seed,
        use_lottery=bool(use_lottery),
        meta={
            "rng_seed": int(rng_seed),
            "tie_break_seed": int(tie_break_seed) if tie_break_seed is not None else None,
            "use_lottery": bool(use_lottery),
        },
    )
    return plan


def settle_and_build_turns(
    *,
    db_path: str,
    plan: DraftOrderPlan,
    settle_db: bool = True,
) -> Dict[str, Any]:
    """Settle picks (protections+swaps) in DB and build the 60-turn list."""
    draft_year = int(plan.draft_year)

    # Ensure baseline pick rows exist (idempotent)
    from league_service import LeagueService
    from league_repo import LeagueRepo

    settlement_events: List[Dict[str, Any]] = []
    if settle_db:
        with LeagueService.open(str(db_path)) as svc:
            # years_ahead is irrelevant for a single settlement call; keep minimal.
            svc.ensure_draft_picks_seeded(draft_year, list(ALL_TEAM_IDS), years_ahead=0)
            settlement_events = svc.settle_draft_year(draft_year, plan.pick_order_by_pick_id)

    # Read updated pick owners for draft_year
    with LeagueRepo(str(db_path)) as repo:
        repo.init_db()
        picks_map = repo.get_draft_picks_map()

    turns: List[DraftTurn] = []
    overall = 0

    # Round 1
    for slot, original_team in enumerate(plan.round1_slot_to_original_team, start=1):
        overall += 1
        pick_id = make_pick_id(draft_year, 1, original_team)
        pick = picks_map.get(pick_id)
        drafting_team = norm_team_id(pick.get("owner_team") if isinstance(pick, dict) else original_team)
        turns.append(
            DraftTurn(
                round=1,
                slot=int(slot),
                overall_no=int(overall),
                pick_id=pick_id,
                original_team=original_team,
                drafting_team=drafting_team,
                attrs={"settled": bool(settle_db)},
            )
        )

    # Round 2
    for slot, original_team in enumerate(plan.round2_slot_to_original_team, start=1):
        overall += 1
        pick_id = make_pick_id(draft_year, 2, original_team)
        pick = picks_map.get(pick_id)
        drafting_team = norm_team_id(pick.get("owner_team") if isinstance(pick, dict) else original_team)
        turns.append(
            DraftTurn(
                round=2,
                slot=int(slot),
                overall_no=int(overall),
                pick_id=pick_id,
                original_team=original_team,
                drafting_team=drafting_team,
                attrs={"settled": bool(settle_db)},
            )
        )

    return {
        "draft_year": draft_year,
        "settlement_events": settlement_events,
        "turns": turns,
    }


def finalize_draft_year(
    state_snapshot: Mapping[str, Any],
    *,
    db_path: Optional[str] = None,
    draft_year: Optional[int] = None,
    rng_seed: int,
    tie_break_seed: Optional[int] = None,
    use_lottery: bool = True,
    settle_db: bool = True,
) -> Dict[str, Any]:
    """Convenience: compute plan from state, settle DB, build turns."""
    plan = compute_plan_from_state(
        state_snapshot,
        draft_year=draft_year,
        rng_seed=int(rng_seed),
        tie_break_seed=tie_break_seed,
        use_lottery=use_lottery,
    )
    dbp = str(db_path) if db_path is not None else infer_db_path_from_state(state_snapshot)
    out = settle_and_build_turns(db_path=dbp, plan=plan, settle_db=bool(settle_db))
    return {
        "draft_year": int(plan.draft_year),
        "plan": plan,
        "settlement_events": out["settlement_events"],
        "turns": out["turns"],
    }
