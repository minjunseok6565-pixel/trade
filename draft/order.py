from __future__ import annotations

"""Draft order construction (pure).

Responsibilities:
  - From team records (regular season) -> determine 1st-round and 2nd-round
    original-slot orders for a given draft_year.
  - 1st round:
      * slots 1..14 belong to bottom-14 teams
      * slots 1..4 via lottery draw (NBA odds)
      * slots 5..14 remaining bottom-14 in worst->best order
      * slots 15..30 for the other 16 teams in worst->best order
  - 2nd round:
      * slots 1..30 for all 30 teams in worst->best order

Note:
  This module ONLY outputs "original order" (original teams per slot).
  Actual drafting team (pick owner) is resolved later via DB (draft.finalize).
"""

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .types import DraftOrderPlan, TeamId, TeamRecord, make_pick_id
from .standings import rank_teams_worst_to_best
from .lottery import run_lottery_top4


def compute_draft_order_plan_from_records(
    *,
    draft_year: int,
    records: Mapping[TeamId, TeamRecord],
    rng_seed: int,
    tie_break_seed: Optional[int] = None,
    use_lottery: bool = True,
    meta: Optional[Dict[str, Any]] = None,
) -> DraftOrderPlan:
    """Compute a DraftOrderPlan from records."""
    draft_year_i = int(draft_year)
    rank = rank_teams_worst_to_best(records, tie_break_seed=tie_break_seed)
    if len(rank) < 30:
        # tolerate partial record dicts by using keys we have
        # (but ordering logic expects 30; caller should provide full 30)
        pass

    bottom14 = tuple(rank[:14])
    top16 = tuple(rank[14:30])

    lottery_result = None
    if use_lottery:
        lottery_result = run_lottery_top4(bottom14, rng_seed=int(rng_seed), include_audit=False)
        winners = list(lottery_result.winners_top4)
        # slots 1..4 winners in drawn order
        slots_1_4 = tuple(winners)
        # remaining bottom-14 in worst->best order, excluding winners
        rest = [t for t in bottom14 if t not in set(winners)]
        slots_5_14 = tuple(rest)
        slots_1_14 = slots_1_4 + slots_5_14
    else:
        slots_1_14 = bottom14

    if len(slots_1_14) != 14:
        raise RuntimeError("round1 bottom14 slots must be length 14")

    slots_15_30 = top16
    if len(slots_15_30) != 16:
        raise RuntimeError("round1 top16 slots must be length 16")

    round1 = tuple(list(slots_1_14) + list(slots_15_30))
    round2 = tuple(rank[:30])

    pick_order_by_pick_id: Dict[str, int] = {}
    for slot, original_team in enumerate(round1, start=1):
        pick_id = make_pick_id(draft_year_i, 1, original_team)
        pick_order_by_pick_id[pick_id] = int(slot)
    for slot, original_team in enumerate(round2, start=1):
        pick_id = make_pick_id(draft_year_i, 2, original_team)
        pick_order_by_pick_id[pick_id] = int(slot)

    return DraftOrderPlan(
        draft_year=draft_year_i,
        records={str(k): v for k, v in dict(records).items()},
        rank_worst_to_best=tuple(rank[:30]),
        round1_slot_to_original_team=tuple(round1),
        round2_slot_to_original_team=tuple(round2),
        pick_order_by_pick_id=pick_order_by_pick_id,
        lottery_result=lottery_result,
        meta=dict(meta or {}),
    )
