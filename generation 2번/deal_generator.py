from __future__ import annotations

"""
deal_generator.py

High-level "deal generator" that sits on top of existing SSOT components:
- TradeGenerationTickContext (tick caches + validate_deal)
- TradeAssetCatalog (tick-scoped outgoing buckets / incoming-by-need indices)

Design goals
------------
- NBA-like: need-driven targeting + plausible packages (players/picks/fillers)
- Fast: tick_ctx cache reuse + bounded search (beam + strict budgets)
- Stable: minimize invalid deals via prefilters + validate(meta)-driven repair
- Consistent: final scoring is ALWAYS based on evaluate_deal_for_team(...) for both sides

Orchestrator usage (example)
----------------------------
    from trades.generation import build_trade_generation_tick_context, DealGenerator, DealGeneratorConfig

    with build_trade_generation_tick_context() as tick_ctx:
        gen = DealGenerator(DealGeneratorConfig())
        proposals = gen.generate_for_team("LAL", tick_ctx, max_results=8)
        # Orchestrator policy:
        # - if both ACCEPT => commit/apply
        # - else keep for counter or discard

Notes
-----
- This generator only produces 2-team deals (teams=[A,B], no to_team set).
  Multi-team deals are left as an extension point.
- Dedupe is canonicalize_deal-based and ignores meta.
- "2nd apron one-for-one" is enforced via validate error method
  (SalaryMatchingRule detail: method == 'second_apron_one_for_one').

"""

from dataclasses import dataclass, field
from typing import Any, Callable, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque
import hashlib
import json
import math
import random
from datetime import date

try:
    from schema import normalize_team_id  # type: ignore
except Exception:  # pragma: no cover
    def normalize_team_id(x: str, strict: bool = False) -> str:  # type: ignore
        return str(x or "").upper()

from ..errors import (
    TradeError,
    DEAL_INVALIDATED,
    ROSTER_LIMIT,
    ASSET_LOCKED,
    PLAYER_NOT_OWNED,
    PICK_NOT_OWNED,
    SWAP_NOT_OWNED,
    SWAP_NOT_FOUND,
    SWAP_INVALID,
    DUPLICATE_ASSET,
)

from ..models import Deal, PlayerAsset, PickAsset, SwapAsset, canonicalize_deal, serialize_deal
from ..valuation.service import evaluate_deal_for_team
from ..valuation.types import DealDecision, TeamDealEvaluation, DealVerdict

from .generation_tick import TradeGenerationTickContext
from .asset_catalog import (
    TradeAssetCatalog,
    TeamOutgoingCatalog,
    PlayerTradeCandidate,
    PickTradeCandidate,
    SwapTradeCandidate,
    IncomingPlayerRef,
    StepienHelper,
    build_trade_asset_catalog,
)



from .dealgen_types import DealGeneratorConfig, DealProposal, DealGenerationStats, _Budgets, _DealSpec, _GenState

class DealGenerator:
    def __init__(self, config: Optional[DealGeneratorConfig] = None) -> None:
        self.config = config or DealGeneratorConfig()
        self.last_stats: Optional[DealGenerationStats] = None
        # Cache for per-call asset catalogs built with allow_locked_by_deal_id.
        #
        # NOTE: TradeGenerationTickContext is a @dataclass(slots=True) and is NOT weakref-able,
        # so we cannot safely use WeakKeyDictionary. Instead we keep a single-tick cache keyed by
        # allow_locked_by_deal_id, and clear it whenever the tick_ctx instance changes.
        self._asset_catalog_cache_tick_id: Optional[int] = None
        self._asset_catalog_cache: Dict[str, TradeAssetCatalog] = {}

    def _get_asset_catalog_for_call(
        self,
        tick_ctx: TradeGenerationTickContext,
        *,
        allow_locked_by_deal_id: Optional[str],
    ) -> Optional[TradeAssetCatalog]:
        # Returns an asset catalog appropriate for this call.
        # - When allow_locked_by_deal_id is None, reuse tick_ctx.asset_catalog (build if missing).
        # - When provided, build (and cache) a catalog that treats assets locked by that deal_id as eligible
        #   for outgoing/incoming indexing.
        if allow_locked_by_deal_id is None:
            if tick_ctx.asset_catalog is None:
                try:
                    tick_ctx.asset_catalog = build_trade_asset_catalog(tick_ctx=tick_ctx)  # type: ignore[arg-type]
                except Exception:
                    return None
            return tick_ctx.asset_catalog

        # Treat empty string as "no allow-locked" to avoid pointless rebuilds.
        if not str(allow_locked_by_deal_id or "").strip():
            if tick_ctx.asset_catalog is None:
                try:
                    tick_ctx.asset_catalog = build_trade_asset_catalog(tick_ctx=tick_ctx)  # type: ignore[arg-type]
                except Exception:
                    return None
            return tick_ctx.asset_catalog

        # Single-tick cache: clear whenever tick_ctx identity changes.
        tick_id = id(tick_ctx)
        if self._asset_catalog_cache_tick_id != tick_id:
            self._asset_catalog_cache_tick_id = tick_id
            self._asset_catalog_cache.clear()

        key = str(allow_locked_by_deal_id)
        cached = self._asset_catalog_cache.get(key)
        if cached is not None:
            return cached

        try:
            cat = build_trade_asset_catalog(tick_ctx=tick_ctx, allow_locked_by_deal_id=allow_locked_by_deal_id)  # type: ignore[arg-type]
        except Exception:
            return None
        self._asset_catalog_cache[key] = cat
        return cat


    def generate_for_team(
        self,
        team_id: str,
        tick_ctx: TradeGenerationTickContext,
        *,
        max_results: int = 10,
        allow_locked_by_deal_id: Optional[str] = None,
        rng_seed: Optional[int] = None,
    ) -> List[DealProposal]:
        """Generate candidate deals for a given team (2-team deals only).

        Modes
        -----
        * BUY mode (default): select incoming targets that match this team's needs and build offers.
        * SELL mode (posture SELL/SOFT_SELL): "shop" this team's outgoing assets to plausible buyers.

        Regardless of mode, hard-rule validity is enforced via tick_ctx.validate_deal and
        final acceptability is judged ONLY via evaluate_deal_for_team for both teams.
        """
        tid = _canon_team_id(team_id)
        ts = tick_ctx.get_team_situation(tid)

        # Early exit: market throttling / stand pat
        if getattr(ts, "constraints", None) is not None:
            if bool(getattr(ts.constraints, "cooldown_active", False)):
                return []
        posture = str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
        if posture == "STAND_PAT" and float(getattr(ts, "urgency", 0.0) or 0.0) < self.config.stand_pat_min_urgency:
            return []

        # Early exit: trade deadline passed (avoid burning validation budgets).
        if _deadline_passed(tick_ctx):
            return []

        # Resolve asset catalog for this call (allow_locked may require a different index snapshot)
        catalog = self._get_asset_catalog_for_call(tick_ctx, allow_locked_by_deal_id=allow_locked_by_deal_id)
        if catalog is None:
            return []

        # Deterministic RNG by default (replay safe)
        seed = int(rng_seed) if rng_seed is not None else _stable_seed(self.config.deterministic_seed_salt, tick_ctx.current_date.isoformat(), tid)
        rng = random.Random(seed)

        stats = DealGenerationStats(team_id=tid)
        self.last_stats = stats

        budgets = _compute_budgets(self.config, ts, max_results=max_results)
        state = _GenState(
            cfg=self.config,
            tick_ctx=tick_ctx,
            catalog=catalog,
            allow_locked_by_deal_id=allow_locked_by_deal_id,
            rng=rng,
            stats=stats,
        )

        if posture in ("SELL", "SOFT_SELL"):
            results = _generate_sell_mode(state, seller_id=tid, budgets=budgets, max_results=max_results)
        else:
            results = _generate_buy_mode(state, buyer_id=tid, budgets=budgets, max_results=max_results)

        # Global sort + trim (helpers already keep things bounded, but be defensive)
        results.sort(key=lambda x: x.score, reverse=True)
        results = results[: max(0, int(max_results))]

        # telemetry hook
        if self.config.telemetry_enabled and self.config.on_stats is not None:
            try:
                self.config.on_stats(stats)
            except Exception:
                pass

        return results


# =============================================================================
# Mode runners (BUY vs SELL)
# =============================================================================
def _generate_buy_mode(state: _GenState, *, buyer_id: str, budgets: _Budgets, max_results: int) -> List[DealProposal]:
    """Standard BUY-mode generation for the initiating team."""
    cfg = state.cfg
    stats = state.stats

    # Select targets (IncomingPlayerRef list)
    targets = _select_targets(state, buyer_id=buyer_id, budgets=budgets)
    targets = targets[: max(0, budgets.max_targets)]

    results: List[DealProposal] = []
    per_target_best: DefaultDict[str, List[DealProposal]] = defaultdict(list)

    for ref in targets:
        seller_id = _canon_team_id(ref.from_team)
        if not seller_id or seller_id == buyer_id:
            continue

        # build deal skeletons for this target
        skeletons = _build_offer_skeletons(state, buyer_id=buyer_id, target_ref=ref)
        attempts = 0

        for sk in skeletons:
            if attempts >= budgets.max_attempts_per_target:
                break
            if stats.validations >= budgets.max_validations or stats.evaluations >= budgets.max_evaluations:
                break

            attempts += 1
            stats.attempts += 1

            # validate / repair
            deal = _repair_until_valid(state, sk, budgets=budgets)
            if deal is None:
                continue

            # dedupe
            fp = _deal_fingerprint_2team(deal)
            if fp in state.seen_fingerprints:
                stats.pruned_duplicate += 1
                continue
            state.seen_fingerprints.add(fp)

            # evaluate + score
            proposal = _evaluate_and_score(state, deal, buyer_id=buyer_id, seller_id=seller_id, partner_id=seller_id)
            if proposal is None:
                continue

            # optional sweetener loop for near-miss
            proposals_to_add = [proposal]
            if cfg.enable_sweeteners:
                proposals_to_add = _sweetener_loop(state, proposal, budgets=budgets, partner_id=seller_id)

            for p in proposals_to_add:
                per_target_best[ref.player_id].append(p)

            # maintain per-target beam
            if per_target_best[ref.player_id]:
                per_target_best[ref.player_id].sort(key=lambda x: x.score, reverse=True)
                per_target_best[ref.player_id] = per_target_best[ref.player_id][: budgets.beam_width]

        # early stop if budgets tight
        if stats.validations >= budgets.max_validations or stats.evaluations >= budgets.max_evaluations:
            break

    for lst in per_target_best.values():
        results.extend(lst)

    results.sort(key=lambda x: x.score, reverse=True)
    results = _apply_partner_cap(state, results, max_results=max_results, partner_side="seller")
    return results


def _generate_sell_mode(state: _GenState, *, seller_id: str, budgets: _Budgets, max_results: int) -> List[DealProposal]:
    """SELL-mode generation: shop this team's outgoing candidates to plausible buyers."""
    cfg = state.cfg
    tick_ctx = state.tick_ctx
    catalog = state.catalog
    stats = state.stats

    seller_id = _canon_team_id(seller_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if seller_out is None:
        return []

    sale_assets = _select_sale_assets(state, seller_id=seller_id, budgets=budgets)
    results: List[DealProposal] = []
    per_asset_best: DefaultDict[str, List[DealProposal]] = defaultdict(list)

    for sale in sale_assets:
        if stats.validations >= budgets.max_validations or stats.evaluations >= budgets.max_evaluations:
            break

        # Pick plausible buyers for this asset.
        buyer_rows = _select_buyers_for_sale_asset(state, seller_id=seller_id, sale_player=sale, budgets=budgets)

        for buyer_id, best_tag, _score in buyer_rows:
            buyer_id = _canon_team_id(buyer_id)
            if not buyer_id or buyer_id == seller_id:
                continue

            # Create a synthetic incoming ref so we can reuse BUY-mode skeleton builder
            ref = IncomingPlayerRef(
                player_id=sale.player_id,
                from_team=seller_id,
                tag=str(best_tag),
                tag_strength=float((sale.supply or {}).get(best_tag, 0.0) or 0.0),
                market_total=float(getattr(getattr(sale, "market", None), "total", 0.0) or 0.0),
                salary_m=float(getattr(sale, "salary_m", 0.0) or 0.0),
                remaining_years=float(getattr(sale, "remaining_years", 0.0) or 0.0),
                age=getattr(getattr(sale, "snap", None), "age", None),
            )

            skeletons = _build_offer_skeletons(state, buyer_id=buyer_id, target_ref=ref)
            attempts = 0

            for sk in skeletons:
                if attempts >= budgets.max_attempts_per_target:
                    break
                if stats.validations >= budgets.max_validations or stats.evaluations >= budgets.max_evaluations:
                    break

                attempts += 1
                stats.attempts += 1

                deal = _repair_until_valid(state, sk, budgets=budgets)
                if deal is None:
                    continue

                fp = _deal_fingerprint_2team(deal)
                if fp in state.seen_fingerprints:
                    stats.pruned_duplicate += 1
                    continue
                state.seen_fingerprints.add(fp)

                # evaluate + score (buyer is the other team; seller is the initiating team)
                proposal = _evaluate_and_score(state, deal, buyer_id=buyer_id, seller_id=seller_id, partner_id=buyer_id)
                if proposal is None:
                    continue

                proposals_to_add = [proposal]
                if cfg.enable_sweeteners:
                    proposals_to_add = _sweetener_loop(state, proposal, budgets=budgets, partner_id=buyer_id)

                for p in proposals_to_add:
                    per_asset_best[sale.player_id].append(p)

                if per_asset_best[sale.player_id]:
                    per_asset_best[sale.player_id].sort(key=lambda x: x.score, reverse=True)
                    per_asset_best[sale.player_id] = per_asset_best[sale.player_id][: budgets.beam_width]

            if stats.validations >= budgets.max_validations or stats.evaluations >= budgets.max_evaluations:
                break

    for lst in per_asset_best.values():
        results.extend(lst)

    results.sort(key=lambda x: x.score, reverse=True)
    results = _apply_partner_cap(state, results, max_results=max_results, partner_side="buyer")
    return results


# =============================================================================
# Split implementation imports (moved out of this file)
# =============================================================================
from .dealgen_budget import _compute_budgets
from .dealgen_utils import (
    _stable_seed,
    _canon_team_id,
    _clamp01,
    _parse_iso_ymd,
    _deadline_passed,
    _is_ban_active,
    _deal_complexity_exceeds,
    _is_locked,
    _deal_fingerprint_2team,
)
from .dealgen_targeting import (
    _select_sale_assets,
    _select_buyers_for_sale_asset,
    _get_need_map,
    _select_targets,
    _need_fit_score,
    _best_need_tag,
    _rank_for_need,
    _sample_for_counterparty,
)
from .dealgen_skeletons import (
    _build_offer_skeletons,
    _buyer_can_absorb_target,
    _collect_buyer_player_candidates,
    _sample_near_salary,
    _picks_packages,
    _apply_partner_cap,
)
from .dealgen_repair import (
    _repair_until_valid,
    _ban_from_error,
    _repair_roster_limit,
    _repair_second_apron_one_for_one,
    _repair_salary_matching,
    _repair_pick_rules,
)
from .dealgen_scoring import (
    _spec_to_deal,
    _evaluate_and_score,
    _score_deal,
    _count_assets,
    _count_players,
    _sigmoid,
    _extract_tags_from_deal,
)
from .dealgen_sweeteners import (
    _sweetener_loop,
    _stepien_ok_after,
    _deal_to_spec_guess,
)
