from __future__ import annotations

"""dealgen.skeletons

Offer skeleton construction mixin.
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
    _sigmoid,
    _deal_num_assets,
    _deal_num_players_moved,
    _deal_outgoing_pick_ids,
    _deal_incoming_pick_ids,
    _deal_assets_by_team,
    _protected_player_ids_from_meta,
    _hash_deal_for_dedupe,
    _safe_float,
    _team_posture,
    _team_time_horizon,
    _team_urgency,
    _team_deadline_pressure,
    _team_cooldown,
    _team_cap_space,
    _team_apron_status,
    _trade_rules,
    _trade_deadline_date,
    _resolve_receiver_team_id_2team,
)
from .need_fit import (
    _is_rebuildish,
    _is_young_candidate,
    _team_need_map,
    _need_fit_score,
)
from .candidates import (
    _pick_from_buckets,
    _closest_salary_players,
    _player_asset,
)
from .salary import (
    _is_one_for_one_mode,
)


class _SkeletonsMixin:
    def _build_offer_skeletons(
        self,
        *,
        buyer_id: str,
        seller_id: str,
        target_player_id: str,
        tag_hint: Optional[str],
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
    ) -> List[Tuple[Deal, Set[str]]]:
        buyer_id = _canon_team_id(buyer_id)
        seller_id = _canon_team_id(seller_id)
        pid = str(target_player_id)
        out: List[Tuple[Deal, Set[str]]] = []

        buyer_ts = tick_ctx.get_team_situation(buyer_id)
        seller_ts = tick_ctx.get_team_situation(seller_id)

        buyer_out = catalog.outgoing_by_team.get(buyer_id)
        seller_out = catalog.outgoing_by_team.get(seller_id)
        if buyer_out is None or seller_out is None:
            return []
        if pid not in seller_out.players:
            return []

        target = seller_out.players[pid]
        target_salary_m = float(target.salary_m)
        target_market = float(target.market.total)

        # Seller need-map used to build "what seller wants to receive"
        seller_need_map = _team_need_map(tick_ctx, seller_id)

        tags_base: Set[str] = set()
        if tag_hint:
            tags_base.add(f"need:{tag_hint}")
        tags_base.add(f"target:{pid}")

        # Determine whether picks-only is viable (cap space absorption)
        cap_space = _team_cap_space(buyer_ts)
        cap_space_m = cap_space / 1_000_000.0
        can_absorb = cap_space_m >= target_salary_m and cap_space_m > 0.25
        if can_absorb:
            d = self._make_base_deal(buyer_id, seller_id, target_player_id=pid)
            added = self._add_picks_package(
                d,
                from_team=buyer_id,
                to_team=seller_id,
                catalog=catalog,
                desired_total_value=max(4.0, 0.75 * target_market),
                max_picks=2,
                include_first_safe=_is_rebuildish(seller_ts),
                rng=rng,
            )
            if added:
                tags = set(tags_base)
                tags.add("archetype:picks_only")
                out.append((d, tags))

        # Young + pick (rebuild sellers)
        if _is_rebuildish(seller_ts) or rng.random() < 0.25:
            young = _pick_from_buckets(
                buyer_out,
                buckets=("SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_CHEAP"),
                exclude_players=set(),
                to_team=seller_id,
                max_n=6,
                prefer_low_market=False,
                receiver_need_map=seller_need_map,
            )
            # Enforce 'youngness' so this archetype matches its label.
            young = [c for c in young if _is_young_candidate(c, self.cfg)]
            if young:
                def _age(c: PlayerTradeCandidate) -> float:
                    try:
                        a = getattr(getattr(c, 'snap', None), 'age', None)
                        return float(a) if a is not None else 99.0
                    except Exception:
                        return 99.0

                def _fit(c: PlayerTradeCandidate) -> float:
                    try:
                        return _need_fit_score(getattr(c, 'supply', None) or {}, seller_need_map)
                    except Exception:
                        return 0.0

                # Prefer younger assets, then seller fit, then higher market (realistic 'young core' value pieces).
                young.sort(key=lambda c: (_age(c), -_fit(c), -float(c.market.total), c.player_id))

                for c in young[:2]:
                    d = self._make_base_deal(buyer_id, seller_id, target_player_id=pid)
                    d.legs[buyer_id].append(_player_asset(c.player_id))
                    # add a small pick sweetener
                    self._add_pick_by_bucket(d, from_team=buyer_id, to_team=seller_id, catalog=catalog, bucket="SECOND")
                    tags = set(tags_base)
                    tags.add("archetype:young_plus_pick")
                    tags.add(f"give:{c.player_id}")
                    out.append((d, tags))

        # Player-for-player around salary
        p2p = _closest_salary_players(
            buyer_out,
            target_salary_m=target_salary_m,
            exclude_players=set(),
            to_team=seller_id,
            max_n=3,
            receiver_need_map=seller_need_map,
        )
        for c in p2p[:2]:
            d = self._make_base_deal(buyer_id, seller_id, target_player_id=pid)
            d.legs[buyer_id].append(_player_asset(c.player_id))
            # optional small pick when value mismatch
            if float(c.market.total) + 2.0 < target_market and rng.random() < 0.75:
                self._add_pick_by_bucket(d, from_team=buyer_id, to_team=seller_id, catalog=catalog, bucket="SECOND")
            tags = set(tags_base)
            tags.add("archetype:p2p")
            tags.add(f"give:{c.player_id}")
            out.append((d, tags))

        # Consolidate (2-for-1) only when neither side is (or will become) 2nd-apron one-for-one.
        buyer_apron = _team_apron_status(buyer_ts)
        seller_apron = _team_apron_status(seller_ts)
        if buyer_apron != "ABOVE_2ND_APRON" and seller_apron != "ABOVE_2ND_APRON" and rng.random() < 0.70:
            cons = _pick_from_buckets(
                buyer_out,
                buckets=("CONSOLIDATE", "SURPLUS_REDUNDANT", "SURPLUS_LOW_FIT"),
                exclude_players=set(),
                to_team=seller_id,
                max_n=3,
                prefer_low_market=False,
                receiver_need_map=seller_need_map,
                # We will add a 2nd outgoing player; exclude solo-only assets up front.
                current_outgoing_players_count=1,
            )
            filler = _pick_from_buckets(
                buyer_out,
                buckets=("FILLER_CHEAP", "FILLER_BAD_CONTRACT", "EXPIRING"),
                exclude_players=set(c.player_id for c in cons),
                to_team=seller_id,
                max_n=4,
                prefer_low_market=True,
                receiver_need_map=seller_need_map,
                current_outgoing_players_count=1,
            )
            if cons and filler:
                d = self._make_base_deal(buyer_id, seller_id, target_player_id=pid)
                d.legs[buyer_id].append(_player_asset(cons[0].player_id))
                d.legs[buyer_id].append(_player_asset(filler[0].player_id))
                # If either side will be 2nd-apron one-for-one *after* this deal, skip 2-for-1 packages.
                if not (
                    _is_one_for_one_mode(deal=d, team_id=buyer_id, tick_ctx=tick_ctx, catalog=catalog)
                    or _is_one_for_one_mode(deal=d, team_id=seller_id, tick_ctx=tick_ctx, catalog=catalog)
                ):
                    if rng.random() < 0.55:
                        self._add_pick_by_bucket(d, from_team=buyer_id, to_team=seller_id, catalog=catalog, bucket="SECOND")
                    tags = set(tags_base)
                    tags.add("archetype:consolidate")
                    tags.add(f"give:{cons[0].player_id}")
                    tags.add(f"give:{filler[0].player_id}")
                    out.append((d, tags))

        # Cap skeleton count
        out = out[: int(budgets.get("skeletons_per_target", 5))]

        # Shuffle slightly but deterministically (rng already seeded)
        rng.shuffle(out)
        return out[: int(budgets.get("skeletons_per_target", 5))]

    def _make_base_deal(self, buyer_id: str, seller_id: str, *, target_player_id: str) -> Deal:
        buyer_id = _canon_team_id(buyer_id)
        seller_id = _canon_team_id(seller_id)
        deal = Deal(
            teams=[buyer_id, seller_id],
            legs={buyer_id: [], seller_id: [_player_asset(target_player_id)]},
            meta={
                "dealgen": {"version": 1, "target_player_id": str(target_player_id)},
                # protected list used by repair to avoid removing the primary target
                "protected_player_ids": [str(target_player_id)],
            },
        )
        return deal
