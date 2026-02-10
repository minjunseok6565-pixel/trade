from __future__ import annotations

"""dealgen.sweeteners

Pick/swap helpers and pick package mixin.
"""

from dataclasses import dataclass, field
from datetime import date
from math import exp
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import hashlib
import json
import random
import re

from ...errors import (
    DEAL_INVALIDATED,
    TRADE_DEADLINE_PASSED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    DUPLICATE_ASSET,
    TradeError,
)
from ...models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ...valuation.service import evaluate_deal_for_team
from ...valuation.types import DealDecision, TeamDealEvaluation

from ..generation_tick import TradeGenerationTickContext
from ..asset_catalog import (
    IncomingPlayerRef,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    TradeAssetCatalog,
    build_trade_asset_catalog,
)

from .utils import (
    _canon_team_id,
    _deal_incoming_pick_ids,
    _deal_outgoing_pick_ids,
)
from .candidates import (
    _pick_asset,
    _swap_asset,
)


class _SweetenersMixin:
    def _add_pick_by_bucket(
        self,
        deal: Deal,
        *,
        from_team: str,
        to_team: str,
        catalog: TradeAssetCatalog,
        bucket: str,
        exclude_pick_ids: Optional[Set[str]] = None,
    ) -> bool:
        from_team_u = _canon_team_id(from_team)
        to_team_u = _canon_team_id(to_team)
        outcat = catalog.outgoing_by_team.get(from_team_u)
        if outcat is None:
            return False
        ids = list(outcat.pick_ids_by_bucket.get(bucket, tuple()) or tuple())
        if not ids:
            return False
        existing = _deal_outgoing_pick_ids(deal, from_team_u)
        excluded = set(exclude_pick_ids or set())
        incoming = _deal_incoming_pick_ids(deal, from_team_u)
        for pid in ids:
            if pid in existing or pid in excluded:
                continue
            # Stepien check (quick)
            if not catalog.stepien.is_compliant_after(team_id=from_team_u, outgoing_pick_ids=existing | {pid}, incoming_pick_ids=incoming):
                continue
            a = _pick_asset(pid, outcat)
            if a is None:
                continue
            deal.legs[from_team_u] = list(deal.legs.get(from_team_u, []) or []) + [a]
            return True
        return False

    def _add_swap_sweetener(self, deal: Deal, *, from_team: str, to_team: str, catalog: TradeAssetCatalog) -> bool:
        from_team_u = _canon_team_id(from_team)
        outcat = catalog.outgoing_by_team.get(from_team_u)
        if outcat is None:
            return False
        existing_swaps = {str(a.swap_id) for a in (deal.legs.get(from_team_u, []) or []) if isinstance(a, SwapAsset)}
        for sid in outcat.swap_ids or tuple():
            if str(sid) in existing_swaps:
                continue
            a = _swap_asset(str(sid), outcat)
            if a is None:
                continue
            deal.legs[from_team_u] = list(deal.legs.get(from_team_u, []) or []) + [a]
            return True
        return False

    def _add_picks_package(
        self,
        deal: Deal,
        *,
        from_team: str,
        to_team: str,
        catalog: TradeAssetCatalog,
        desired_total_value: float,
        max_picks: int,
        include_first_safe: bool,
        rng: random.Random,
    ) -> bool:
        """Add a small picks package from from_team to to_team.

        Returns True if at least one pick was added.
        """
        from_team_u = _canon_team_id(from_team)
        outcat = catalog.outgoing_by_team.get(from_team_u)
        if outcat is None:
            return False
        existing = _deal_outgoing_pick_ids(deal, from_team_u)
        incoming = _deal_incoming_pick_ids(deal, from_team_u)
        rejected: Set[str] = set()  # picks tried and rejected (e.g., Stepien)

        # Candidate picks ordered by bucket preference.
        buckets: List[str] = ["SECOND"]
        if include_first_safe:
            buckets.append("FIRST_SAFE")
        # First sensitive only if config allows
        if self.cfg.allow_first_sensitive_as_last_resort:
            buckets.append("FIRST_SENSITIVE")

        added_any = False
        total_value = 0.0
        # Try add up to max_picks
        for _ in range(int(max_picks)):
            best: Optional[PickTradeCandidate] = None
            best_bucket: Optional[str] = None
            for b in buckets:
                for pid in list(outcat.pick_ids_by_bucket.get(b, tuple()) or tuple()):
                    if pid in existing or pid in rejected:
                        continue
                    cand = outcat.picks.get(pid)
                    if cand is None:
                        continue
                    # prefer smaller incremental value as we approach target
                    if best is None:
                        best = cand
                        best_bucket = b
                        continue
                    # pick the one that gets us closer to desired_total_value
                    cur_diff = abs((total_value + best.market.total) - desired_total_value)
                    new_diff = abs((total_value + cand.market.total) - desired_total_value)
                    if new_diff < cur_diff:
                        best = cand
                        best_bucket = b
            if best is None:
                break

            pid = str(best.pick_id)
            if not catalog.stepien.is_compliant_after(team_id=from_team_u, outgoing_pick_ids=existing | {pid}, incoming_pick_ids=incoming):
                # if this pick makes Stepien illegal, skip it.
                # IMPORTANT: do NOT add it to `existing` because it is not actually in the deal.
                rejected.add(pid)
                continue

            a = _pick_asset(pid, outcat)
            if a is None:
                break

            deal.legs[from_team_u] = list(deal.legs.get(from_team_u, []) or []) + [a]
            existing.add(pid)
            total_value += float(best.market.total)
            added_any = True
            if total_value >= desired_total_value * (0.90 + 0.10 * rng.random()):
                break

        return added_any
