from __future__ import annotations

"""dealgen.targeting

Target selection mixin.
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
    _choose_top_need_tags,
)


class _TargetingMixin:
    def _select_target_pairs(
        self,
        team_id: str,
        *,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        posture: str,
        urgency: float,
        budgets: Mapping[str, int],
        rng: random.Random,
    ) -> List[Tuple[str, str, str, Optional[str]]]:
        """Return list of (buyer_id, seller_id, target_player_id, tag_hint)."""

        p = str(posture or "").upper()
        if p in {"SELL", "SOFT_SELL"}:
            return self._select_target_pairs_sell_mode(
                team_id,
                tick_ctx=tick_ctx,
                catalog=catalog,
                budgets=budgets,
                rng=rng,
            )
        return self._select_target_pairs_buy_mode(
            team_id,
            tick_ctx=tick_ctx,
            catalog=catalog,
            budgets=budgets,
            rng=rng,
        )

    def _select_target_pairs_buy_mode(
        self,
        buyer_id: str,
        *,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
    ) -> List[Tuple[str, str, str, Optional[str]]]:
        need_tags = _choose_top_need_tags(tick_ctx, buyer_id, self.cfg)
        if not need_tags:
            return []

        buyer_ts = tick_ctx.get_team_situation(buyer_id)
        buyer_apron = _team_apron_status(buyer_ts)

        # Seller bucket membership cache to avoid repeated set(...) allocations.
        seller_bucket_cache: Dict[str, Tuple[Set[str], Set[str]]] = {}

        # Collect refs from incoming indices.
        refs: List[IncomingPlayerRef] = []
        for tag in need_tags:
            refs.extend(list((catalog.incoming_by_need_tag.get(tag) or tuple())[: int(self.cfg.per_tag_take)]))
            refs.extend(list((catalog.incoming_cheap_by_need_tag.get(tag) or tuple())[: int(self.cfg.cheap_per_tag_take)]))

        # Score refs lightly.
        dc = tick_ctx.get_decision_context(buyer_id)
        need_map = getattr(dc, "need_map", None) or {}
        scored: List[Tuple[float, IncomingPlayerRef]] = []
        for r in refs:
            seller_id = _canon_team_id(r.from_team)
            if not seller_id or seller_id == buyer_id:
                continue
            seller_ts = tick_ctx.get_team_situation(seller_id)
            if _team_cooldown(seller_ts):
                continue

            # Only consider targets that seller is plausibly willing to move.
            seller_out = catalog.outgoing_by_team.get(seller_id)
            if seller_out is None:
                continue
            if r.player_id not in seller_out.players:
                continue

            cached = seller_bucket_cache.get(seller_id)
            if cached is None:
                core_set = set(seller_out.player_ids_by_bucket.get("CORE", tuple()) or tuple())
                offer_set: Set[str] = set()
                for b in (
                    "VETERAN_SALE",
                    "EXPIRING",
                    "SURPLUS_LOW_FIT",
                    "SURPLUS_REDUNDANT",
                    "FILLER_BAD_CONTRACT",
                    "FILLER_CHEAP",
                    "CONSOLIDATE",
                ):
                    offer_set.update(seller_out.player_ids_by_bucket.get(b, tuple()) or tuple())
                seller_bucket_cache[seller_id] = (core_set, offer_set)
            else:
                core_set, offer_set = cached

            # Exclude seller CORE.
            if r.player_id in core_set:
                continue

            # Prefer listed outgoing buckets.
            if r.player_id not in offer_set:
                # still allow a small chance for mid-tier movement
                if rng.random() > 0.15:
                    continue

            target_cand = seller_out.players.get(r.player_id)
            if target_cand is None:
                continue
            # return-to-trading-team ban: buyer can't be in banned teams
            if any(_canon_team_id(t) == buyer_id for t in (target_cand.return_ban_teams or tuple())):
                continue

            # If buyer is above 2nd apron, prefer smaller salary targets to avoid hard-to-match.
            apron_pen = 0.0
            if buyer_apron == "ABOVE_2ND_APRON" and float(target_cand.salary_m) >= 15.0:
                apron_pen = 0.45

            need_w = float(need_map.get(r.tag, 0.0) or 0.0)
            score = float(r.tag_strength) * (0.7 + 0.9 * need_w) + 0.15 * float(target_cand.market.total)
            score -= 0.08 * float(target_cand.salary_m)
            score -= apron_pen
            scored.append((score, r))

        scored.sort(key=lambda x: (-x[0], x[1].from_team, x[1].player_id))
        max_targets = int(budgets.get("max_targets", 0))
        max_targets = max(0, max_targets)

        out: List[Tuple[str, str, str, Optional[str]]] = []
        seen: Set[Tuple[str, str]] = set()
        for _, r in scored:
            seller_id = _canon_team_id(r.from_team)
            key = (seller_id, str(r.player_id))
            if key in seen:
                continue
            seen.add(key)
            out.append((buyer_id, seller_id, str(r.player_id), str(r.tag) if r.tag else None))
            if len(out) >= max_targets:
                break

        return out

    def _select_target_pairs_sell_mode(
        self,
        seller_id: str,
        *,
        tick_ctx: TradeGenerationTickContext,
        catalog: TradeAssetCatalog,
        budgets: Mapping[str, int],
        rng: random.Random,
    ) -> List[Tuple[str, str, str, Optional[str]]]:
        """Shop the seller's outgoing players to likely buyers."""
        seller_out = catalog.outgoing_by_team.get(seller_id)
        if seller_out is None:
            return []

        # Choose a small set of "for sale" players.
        candidate_ids: List[str] = []
        for b in ("VETERAN_SALE", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "FILLER_BAD_CONTRACT", "CONSOLIDATE"):
            candidate_ids.extend(list(seller_out.player_ids_by_bucket.get(b, tuple()) or tuple()))
        # de-dupe
        uniq: List[str] = []
        sset: Set[str] = set()
        for pid in candidate_ids:
            if pid in sset:
                continue
            sset.add(pid)
            uniq.append(pid)

        # Small ranking: prefer higher value sale pieces.
        pieces: List[PlayerTradeCandidate] = []
        for pid in uniq:
            c = seller_out.players.get(pid)
            if c is None:
                continue
            pieces.append(c)
        pieces.sort(key=lambda c: (-float(c.market.total), -float(c.salary_m), c.player_id))
        pieces = pieces[: max(0, int(budgets.get("max_targets", 0)))]

        # For each piece, choose likely buyers by scanning teams (30 teams; bounded).
        out: List[Tuple[str, str, str, Optional[str]]] = []
        teams = list(catalog.outgoing_by_team.keys())
        for piece in pieces:
            tag_hint = piece.top_tags[0] if piece.top_tags else None

            scored_buyers: List[Tuple[float, str]] = []
            for buyer_id in teams:
                buyer_id_u = _canon_team_id(buyer_id)
                if not buyer_id_u or buyer_id_u == seller_id:
                    continue
                buyer_ts = tick_ctx.get_team_situation(buyer_id_u)
                if _team_cooldown(buyer_ts):
                    continue
                # return-to-team ban
                if buyer_id_u in {str(t).upper() for t in (piece.return_ban_teams or tuple())}:
                    continue
                dc = tick_ctx.get_decision_context(buyer_id_u)
                nm = getattr(dc, "need_map", None) or {}
                if not isinstance(nm, Mapping):
                    continue
                need_fit = 0.0
                for tag, sv in (piece.supply or {}).items():
                    need_fit += float(sv or 0.0) * float(nm.get(tag, 0.0) or 0.0)
                if need_fit <= 0.05:
                    continue
                # prefer buy-ish teams
                bp = _team_posture(buyer_ts)
                posture_bonus = 0.12 if bp in {"AGGRESSIVE_BUY", "SOFT_BUY"} else (-0.05 if bp in {"SELL", "SOFT_SELL"} else 0.0)
                score = float(need_fit) + 0.05 * float(piece.market.total) + posture_bonus
                scored_buyers.append((score, buyer_id_u))

            scored_buyers.sort(key=lambda x: (-x[0], x[1]))
            # Pick top N buyers per piece
            for _, buyer_id_u in scored_buyers[:5]:
                out.append((buyer_id_u, seller_id, piece.player_id, tag_hint))
                if len(out) >= int(budgets.get("max_targets", 0)):
                    break
            if len(out) >= int(budgets.get("max_targets", 0)):
                break

        # Small shuffle for variety
        rng.shuffle(out)
        return out[: int(budgets.get("max_targets", 0))]
