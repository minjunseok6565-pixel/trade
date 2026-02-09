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


# =============================================================================
# Public types
# =============================================================================
@dataclass(frozen=True, slots=True)
class DealGeneratorConfig:
    """
    Tuning knobs for search budget + realism constraints.

    Tip: keep hard upper bounds conservative. In a game tick, deal generation should
    never be an unbounded search.
    """

    # --- early exit ---
    stand_pat_min_urgency: float = 0.35

    # --- hard budgets (upper bounds) ---
    max_targets: int = 26
    max_attempts_per_target: int = 60
    max_validations: int = 450
    max_evaluations: int = 260
    max_repairs: int = 2

    # --- complexity constraints ---
    max_assets: int = 6
    max_players_moved: int = 4  # total players moved across both teams

    # --- beam ---
    beam_width: int = 12

    # --- archetype controls ---
    enable_consolidate_2for1: bool = True
    enable_picks_only: bool = True
    enable_sweeteners: bool = True

    # --- sweetener loop ---
    max_sweeteners: int = 2
    near_miss_margin_max: float = 2.0  # try sweetener if seller margin in [-near_miss_margin_max, 0)
    sweetener_order: Tuple[str, ...] = ("SECOND", "SWAP", "FIRST_SAFE", "SECOND", "FIRST_SENSITIVE")

    # --- target selection ---
    max_need_tags: int = 4
    cheap_incoming_bonus: float = 0.25  # slight bump for cheap targets
    target_salary_penalty_scale: float = 0.12  # penalty per $10M (very light)

    # --- scoring ---
    sigmoid_scale: float = 3.5
    complexity_penalty_assets: float = 0.15
    complexity_penalty_players: float = 0.10
    buyer_overpay_penalty: float = 1.0

    # --- market spam control ---
    partner_repeat_penalty: float = 0.35  # score penalty per extra repetition
    max_partner_repeats: int = 3

    # --- deterministic randomness ---
    deterministic_seed_salt: str = "dealgen_v1"

    # --- telemetry ---
    telemetry_enabled: bool = True
    on_stats: Optional[Callable[["DealGenerationStats"], None]] = None

    # --- posture-based recommended baselines (used by _compute_budgets) ---
    posture_targets: Mapping[str, int] = field(
        default_factory=lambda: {
            "AGGRESSIVE_BUY": 22,
            "SOFT_BUY": 14,
            "STAND_PAT": 4,
            "SOFT_SELL": 16,
            "SELL": 18,
        }
    )
    posture_beam: Mapping[str, int] = field(
        default_factory=lambda: {
            "AGGRESSIVE_BUY": 12,
            "SOFT_BUY": 8,
            "STAND_PAT": 5,
            "SOFT_SELL": 9,
            "SELL": 10,
        }
    )


@dataclass(frozen=True, slots=True)
class DealProposal:
    deal: Deal
    buyer_id: str
    seller_id: str

    buyer_decision: DealDecision
    seller_decision: DealDecision
    buyer_eval: TeamDealEvaluation
    seller_eval: TeamDealEvaluation

    score: float
    tags: Tuple[str, ...] = tuple()


@dataclass(slots=True)
class DealGenerationStats:
    """Lightweight counters for tuning + debugging invalid exploration."""

    team_id: str
    validations: int = 0
    evaluations: int = 0
    attempts: int = 0

    # prunes
    pruned_duplicate: int = 0
    pruned_locked: int = 0
    pruned_ineligible: int = 0
    pruned_stepien: int = 0

    # failures
    fail_by_code: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))
    fail_by_rule: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))
    fail_by_method: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))

    # partners
    partner_counts: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))

    # sweetener loop
    sweetener_trials: int = 0
    sweetener_commits: int = 0
    sweetener_rollbacks: int = 0
    sweetener_commit_by_token: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))

    # pick rule prechecks
    stepien_precheck_blocked: int = 0
    stepien_soft_drops: int = 0

    def record_error(self, err: TradeError) -> None:
        self.fail_by_code[str(err.code)] += 1
        details = getattr(err, "details", None)
        if isinstance(details, dict):
            rule = details.get("rule")
            method = details.get("method")
            if rule:
                self.fail_by_rule[str(rule)] += 1
            if method:
                self.fail_by_method[str(method)] += 1

    def snapshot(self) -> Dict[str, Any]:
        return {
            "team_id": self.team_id,
            "validations": self.validations,
            "evaluations": self.evaluations,
            "attempts": self.attempts,
            "pruned_duplicate": self.pruned_duplicate,
            "pruned_locked": self.pruned_locked,
            "pruned_ineligible": self.pruned_ineligible,
            "pruned_stepien": self.pruned_stepien,
            "fail_by_code": dict(self.fail_by_code),
            "fail_by_rule": dict(self.fail_by_rule),
            "fail_by_method": dict(self.fail_by_method),
            "partner_counts": dict(self.partner_counts),
            "sweetener_trials": self.sweetener_trials,
            "sweetener_commits": self.sweetener_commits,
            "sweetener_rollbacks": self.sweetener_rollbacks,
            "sweetener_commit_by_token": dict(self.sweetener_commit_by_token),
            "stepien_precheck_blocked": self.stepien_precheck_blocked,
            "stepien_soft_drops": self.stepien_soft_drops,
        }

# =============================================================================
@dataclass(slots=True)
class _Budgets:
    max_targets: int
    beam_width: int
    max_attempts_per_target: int
    max_validations: int
    max_evaluations: int
    max_repairs: int


@dataclass(slots=True)
class _DealSpec:
    """Intermediate representation before converting to Deal."""
    buyer_id: str
    seller_id: str

    buyer_players_out: List[str] = field(default_factory=list)
    buyer_picks_out: List[str] = field(default_factory=list)
    buyer_swaps_out: List[str] = field(default_factory=list)

    seller_players_out: List[str] = field(default_factory=list)
    seller_picks_out: List[str] = field(default_factory=list)
    seller_swaps_out: List[str] = field(default_factory=list)

    tags: List[str] = field(default_factory=list)

    def copy(self) -> "_DealSpec":
        return _DealSpec(
            buyer_id=self.buyer_id,
            seller_id=self.seller_id,
            buyer_players_out=list(self.buyer_players_out),
            buyer_picks_out=list(self.buyer_picks_out),
            buyer_swaps_out=list(self.buyer_swaps_out),
            seller_players_out=list(self.seller_players_out),
            seller_picks_out=list(self.seller_picks_out),
            seller_swaps_out=list(self.seller_swaps_out),
            tags=list(self.tags),
        )


@dataclass(slots=True)
class _GenState:
    cfg: DealGeneratorConfig
    tick_ctx: TradeGenerationTickContext
    catalog: TradeAssetCatalog
    allow_locked_by_deal_id: Optional[str]
    rng: random.Random
    stats: DealGenerationStats

    # banlists from fatal validation failures: skip in future
    banned_players: DefaultDict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    banned_picks: DefaultDict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    banned_swaps: DefaultDict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))

    # per-player receiver bans learned from validation (e.g., return-to-trading-team same season)
    # keyed by player_id -> set(receiver_team_id)
    banned_receivers_by_player: DefaultDict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))

    # dedupe across generated deals
    seen_fingerprints: Set[str] = field(default_factory=set)


# =============================================================================
# DealGenerator
# =============================================================================
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


def _select_sale_assets(state: _GenState, *, seller_id: str, budgets: _Budgets) -> List[PlayerTradeCandidate]:
    """Pick outgoing assets to shop in SELL mode (surplus/expiring/vet-sale first)."""
    tick_ctx = state.tick_ctx
    catalog = state.catalog
    rng = state.rng

    seller_id = _canon_team_id(seller_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if seller_out is None:
        return []

    ts = tick_ctx.get_team_situation(seller_id)
    posture = str(getattr(ts, "trade_posture", "SELL") or "SELL").upper()

    # Priorities roughly emulate NBA: expiring/vet-sale/surplus are most likely to be shopped.
    bucket_pri = {
        "VETERAN_SALE": 0,
        "EXPIRING": 1,
        "SURPLUS_LOW_FIT": 2,
        "SURPLUS_REDUNDANT": 3,
        "FILLER_CHEAP": 4,
        "FILLER_BAD_CONTRACT": 5,
        "CONSOLIDATE": 6,
        "CORE": 99,
    }

    rows: List[Tuple[int, float, float, float, str, PlayerTradeCandidate]] = []
    for pid, c in seller_out.players.items():
        if pid in state.banned_players[seller_id]:
            continue
        if _is_locked(c.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            continue
        if _is_ban_active(tick_ctx.current_date, c.recent_signing_banned_until):
            continue

        # Exclude CORE in SOFT_SELL; allow ultra-rare CORE in SELL
        if "CORE" in (c.buckets or ()):
            if posture != "SELL":
                continue
            if rng.random() > 0.04:
                continue

        pri = min(bucket_pri.get(b, 50) for b in (c.buckets or ("FILLER_CHEAP",)))
        surplus = float(getattr(c, "surplus_score", 0.0) or 0.0)
        exp = 1.0 if bool(getattr(c, "is_expiring", False)) else 0.0
        value = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        rows.append((pri, -surplus, -exp, value, c.player_id, c))

    rows.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
    assets = [r[-1] for r in rows]

    # Small shuffle among close ranks to avoid "same shopping list" every tick.
    head = assets[: max(6, min(len(assets), budgets.max_targets))]
    rng.shuffle(head)
    tail = assets[len(head):]
    out = head + tail

    return out[: max(0, budgets.max_targets)]


def _select_buyers_for_sale_asset(
    state: _GenState,
    *,
    seller_id: str,
    sale_player: PlayerTradeCandidate,
    budgets: _Budgets,
) -> List[Tuple[str, str, float]]:
    """For a given sale player, pick plausible buyers based on their need_map."""
    tick_ctx = state.tick_ctx
    catalog = state.catalog

    seller_id = _canon_team_id(seller_id)
    max_buyers = max(4, min(14, 2 * budgets.beam_width))

    rows: List[Tuple[float, str, str]] = []
    tags = list(getattr(sale_player, "top_tags", ()) or ())
    supply = getattr(sale_player, "supply", {}) or {}

    for tid in catalog.outgoing_by_team.keys():
        buyer_id = _canon_team_id(tid)
        if not buyer_id or buyer_id == seller_id:
            continue

        buyer_ts = tick_ctx.get_team_situation(buyer_id)
        if bool(getattr(getattr(buyer_ts, "constraints", None), "cooldown_active", False)):
            continue

        need_map = _get_need_map(tick_ctx, buyer_id)
        if not need_map:
            continue

        best_tag = ""
        best = 0.0
        for t in tags:
            w = float(need_map.get(t, 0.0) or 0.0)
            s = float(supply.get(t, 0.0) or 0.0)
            sc = w * (0.4 + 0.6 * s)
            if sc > best:
                best = sc
                best_tag = t

        if best <= 0.05:
            continue

        # Lightly prefer teams with higher urgency to mimic deadline activity.
        urg = float(getattr(buyer_ts, "urgency", 0.0) or 0.0)
        rows.append((best * (0.85 + 0.30 * _clamp01(urg)), buyer_id, best_tag))

    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(bid, tag, float(sc)) for sc, bid, tag in rows[:max_buyers]]
# =============================================================================
# Budgeting + seeds
# =============================================================================
def _compute_budgets(cfg: DealGeneratorConfig, ts: Any, *, max_results: int) -> _Budgets:
    posture = str(getattr(ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
    urgency = float(getattr(ts, "urgency", 0.5) or 0.5)
    deadline_pressure = 0.0
    try:
        deadline_pressure = float(getattr(getattr(ts, "constraints", None), "deadline_pressure", 0.0) or 0.0)
    except Exception:
        deadline_pressure = 0.0

    base_targets = int(cfg.posture_targets.get(posture, 10))
    base_beam = int(cfg.posture_beam.get(posture, cfg.beam_width))

    # scale: urgency & deadline add a bit
    scale = 0.75 + 0.55 * _clamp01(urgency) + 0.35 * _clamp01(deadline_pressure)
    # stand pat stays tight
    if posture == "STAND_PAT":
        scale = min(scale, 0.9)

    max_targets = min(cfg.max_targets, max(0, int(round(base_targets * scale))))
    beam_width = max(2, min(cfg.beam_width, max(2, int(round(base_beam * (0.8 + 0.35 * _clamp01(urgency)))))))

    # budgets scale slightly with desired results
    res_scale = 0.8 + 0.06 * max(0, min(20, int(max_results)))

    return _Budgets(
        max_targets=max_targets,
        beam_width=beam_width,
        max_attempts_per_target=max(10, min(cfg.max_attempts_per_target, int(round(cfg.max_attempts_per_target * scale)))),
        max_validations=max(80, min(cfg.max_validations, int(round(cfg.max_validations * scale * res_scale)))),
        max_evaluations=max(60, min(cfg.max_evaluations, int(round(cfg.max_evaluations * scale * res_scale)))),
        max_repairs=max(0, min(cfg.max_repairs, 3)),
    )


def _stable_seed(salt: str, *parts: str) -> int:
    h = hashlib.blake2b(digest_size=8)
    h.update(str(salt).encode("utf-8"))
    for p in parts:
        h.update(b"|")
        h.update(str(p).encode("utf-8"))
    return int.from_bytes(h.digest(), "big", signed=False)


def _canon_team_id(team_id: Any) -> str:
    raw = str(team_id or "").strip()
    if not raw:
        return ""
    try:
        return str(normalize_team_id(raw, strict=False)).strip().upper()
    except Exception:
        return raw.upper()


def _clamp01(x: float) -> float:
    try:
        if x < 0.0:
            return 0.0
        if x > 1.0:
            return 1.0
        return float(x)
    except Exception:
        return 0.5


def _parse_iso_ymd(value: object) -> Optional[date]:
    """Parse YYYY-MM-DD (or datetime ISO) into a date. Returns None on failure."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _is_ban_active(current_date: date, until_iso: Optional[str]) -> bool:
    """True if a banned-until ISO string is active at current_date."""
    d = _parse_iso_ymd(until_iso)
    return bool(d is not None and current_date < d)


# =============================================================================
# Target selection
# =============================================================================
def _get_need_map(tick_ctx: TradeGenerationTickContext, team_id: str) -> Dict[str, float]:
    """Best-effort need_map for a team.

    Primary source: tick_ctx.get_decision_context(team_id).need_map (SSOT for valuation).
    Fallback: tick_ctx.get_team_situation(team_id).needs -> {tag: weight}
    """
    tid = _canon_team_id(team_id)
    out: Dict[str, float] = {}
    try:
        dc = tick_ctx.get_decision_context(tid)
        nm = getattr(dc, "need_map", {}) or {}
        if isinstance(nm, dict):
            for k, v in nm.items():
                if not k:
                    continue
                try:
                    out[str(k)] = float(v)
                except Exception:
                    continue
    except Exception:
        pass

    if out:
        return out

    # Fallback
    try:
        ts = tick_ctx.get_team_situation(tid)
        needs = getattr(ts, "needs", None)
        if isinstance(needs, list):
            for n in needs:
                tag = getattr(n, "tag", None)
                w = getattr(n, "weight", None)
                if not tag:
                    continue
                try:
                    out[str(tag)] = float(w)
                except Exception:
                    continue
    except Exception:
        pass

    return out

def _select_targets(state: _GenState, *, buyer_id: str, budgets: _Budgets) -> List[IncomingPlayerRef]:
    cfg = state.cfg
    tick_ctx = state.tick_ctx
    catalog = state.catalog
    buyer_dc = tick_ctx.get_decision_context(buyer_id)
    need_map = _get_need_map(tick_ctx, buyer_id)

    # pick top N tags
    need_items = [(str(k), float(v)) for k, v in need_map.items() if k and v is not None]
    need_items.sort(key=lambda x: x[1], reverse=True)
    need_items = need_items[: max(0, int(cfg.max_need_tags))]

    if not need_items:
        return []

    # candidate pool union
    refs: List[IncomingPlayerRef] = []
    seen_players: Set[str] = set()
    for tag, w in need_items:
        pool = list(catalog.incoming_by_need_tag.get(tag, ()))
        cheap_pool = list(catalog.incoming_cheap_by_need_tag.get(tag, ()))
        # Prefer cheap pool a bit (role players / expiring)
        pool = pool + cheap_pool
        for r in pool:
            if not r.player_id or r.player_id in seen_players:
                continue
            if _canon_team_id(r.from_team) == buyer_id:
                continue
            # seller cooldown filter (avoid spam)
            seller_ts = tick_ctx.get_team_situation(_canon_team_id(r.from_team))
            if bool(getattr(getattr(seller_ts, "constraints", None), "cooldown_active", False)):
                continue

            seen_players.add(r.player_id)
            refs.append(r)

    # rank (lightweight heuristic only; final accept determined later)
    def _rank_key(r: IncomingPlayerRef) -> Tuple[float, float, float, str]:
        # need strength
        w = float(need_map.get(r.tag, 0.0) or 0.0)
        tag_strength = float(getattr(r, "tag_strength", 0.0) or 0.0)
        base = tag_strength * (0.4 + 0.6 * _clamp01(w)) * (0.6 + 0.6 * _clamp01(getattr(buyer_dc, "urgency", 0.5)))
        # cheap bonus
        cheap_bonus = cfg.cheap_incoming_bonus if r in catalog.incoming_cheap_by_need_tag.get(r.tag, ()) else 0.0
        # light salary penalty: high salaries complicate matching
        sal_pen = cfg.target_salary_penalty_scale * (float(getattr(r, "salary_m", 0.0) or 0.0) / 10.0)
        score = base + cheap_bonus - sal_pen
        # tie-break: market_total, then lower salary, then player_id
        return (score, float(getattr(r, "market_total", 0.0) or 0.0), -float(getattr(r, "salary_m", 0.0) or 0.0), str(r.player_id))

    refs.sort(key=_rank_key, reverse=True)
    return refs[: max(0, budgets.max_targets)]


# =============================================================================
# Skeleton generation (archetypes)
# =============================================================================
def _build_offer_skeletons(state: _GenState, *, buyer_id: str, target_ref: IncomingPlayerRef) -> List[_DealSpec]:
    cfg = state.cfg
    catalog = state.catalog
    tick_ctx = state.tick_ctx
    rng = state.rng

    seller_id = _canon_team_id(target_ref.from_team)
    if not seller_id:
        return []

    buyer_out = catalog.outgoing_by_team.get(buyer_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if buyer_out is None or seller_out is None:
        return []

    # resolve full target candidate (the player seller would send out)
    target = seller_out.players.get(target_ref.player_id)
    if target is None:
        return []

    # --- counterpart posture/horizon (used to shape archetypes) ---
    seller_ts = tick_ctx.get_team_situation(seller_id)
    seller_posture = str(getattr(seller_ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
    seller_horizon = str(getattr(seller_ts, "time_horizon", "RE_TOOL") or "RE_TOOL").upper()
    rebuildish = (seller_horizon == "REBUILD") or (seller_posture in ("SELL", "SOFT_SELL"))
    win_nowish = (seller_horizon == "WIN_NOW")

    # Light prefilter: avoid CORE unless seller is SELL-ish. Even then, make it rare.
    if "CORE" in (target.buckets or ()):
        if seller_posture not in ("SELL", "SOFT_SELL"):
            return []
        if rng.random() > 0.10:
            return []

    # lock / return-ban / eligibility prefilter using catalog snapshot
    if _is_locked(target.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
        state.stats.pruned_locked += 1
        return []
    if target_ref.from_team and buyer_id in set(target.return_ban_teams or ()):
        state.stats.pruned_ineligible += 1
        return []

    if buyer_id in state.banned_receivers_by_player.get(target.player_id, set()):
        state.stats.pruned_ineligible += 1
        return []
    if _is_ban_active(tick_ctx.current_date, target.recent_signing_banned_until):
        state.stats.pruned_ineligible += 1
        return []

    # --- Hard realism guard: if either side is above 2nd apron, avoid multi-player constructions up front.
    buyer_ts = tick_ctx.get_team_situation(buyer_id)
    buyer_apron = str(getattr(getattr(buyer_ts, "constraints", None), "apron_status", "") or "")
    seller_apron = str(getattr(getattr(seller_ts, "constraints", None), "apron_status", "") or "")
    apron_one_for_one_hint = (buyer_apron == "ABOVE_2ND_APRON") or (seller_apron == "ABOVE_2ND_APRON")

    # Seller need map guides "return player" selection (NBA feel: they ask for fits, not random bodies)
    seller_need_map = _get_need_map(tick_ctx, seller_id)

    # Build candidate sets for buyer outgoing
    buyer_players = _collect_buyer_player_candidates(state, buyer_out, receiver_team_id=seller_id)
    filler = buyer_players["filler"]
    match = buyer_players["match"]
    young = buyer_players["young"]
    cons = buyer_players["consolidate"]

    # Archetype shaping by seller horizon (still bounded by shuffle + final cap)
    p4p_k = 3 if win_nowish else 2
    salary_k = 3 if win_nowish else 2
    picks_pkg_n = 4 if rebuildish else 2
    young_k = 2 if rebuildish else 1
    young_pkg_n = 3 if rebuildish else 1

    enable_2for1 = bool(cfg.enable_consolidate_2for1) and (not apron_one_for_one_hint) and win_nowish

    skeletons: List[_DealSpec] = []

    # --- archetype: player-for-player (return player chosen to match seller needs when possible)
    p4p_pool = _sample_for_counterparty(match, target.salary_m, need_map=seller_need_map, rng=rng, k=p4p_k)
    for p in p4p_pool:
        sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
        sk.seller_players_out = [target.player_id]
        sk.buyer_players_out = [p.player_id]
        sk.tags.append("archetype:p4p")
        sk.tags.append(f"need:{target_ref.tag}")
        if seller_horizon:
            sk.tags.append(f"seller_horizon:{seller_horizon}")
        rt = _best_need_tag(seller_need_map, p)
        if rt:
            sk.tags.append(f"return_need:{rt}")
        skeletons.append(sk)

    # --- archetype: picks-only (rebuildish sellers prefer; win-now sellers rare)
    if cfg.enable_picks_only and _buyer_can_absorb_target(tick_ctx, buyer_id, target.salary_m):
        max_pkg = picks_pkg_n if rebuildish else 1
        for picks, swaps, tag in _picks_packages(state, buyer_out, max_packages=max_pkg):
            sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
            sk.seller_players_out = [target.player_id]
            sk.buyer_picks_out = list(picks)
            sk.buyer_swaps_out = list(swaps)
            sk.tags.append("archetype:picks_only")
            sk.tags.append(tag)
            sk.tags.append(f"need:{target_ref.tag}")
            if seller_horizon:
                sk.tags.append(f"seller_horizon:{seller_horizon}")
            skeletons.append(sk)

    # --- archetype: young + pick (rebuildish / re-tool sellers lean this way)
    if young:
        max_young_players = young_k if rebuildish else 1
        max_young_pkgs = young_pkg_n if rebuildish else 1
        for p in _sample_for_counterparty(young[: max(1, 2 * max_young_players)], target.salary_m, need_map=seller_need_map, rng=rng, k=max_young_players):
            for picks, swaps, tag in _picks_packages(state, buyer_out, max_packages=max_young_pkgs, prefer_second=True):
                sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
                sk.seller_players_out = [target.player_id]
                sk.buyer_players_out = [p.player_id]
                sk.buyer_picks_out = list(picks)
                sk.buyer_swaps_out = list(swaps)
                sk.tags.append("archetype:young+pick")
                sk.tags.append(tag)
                sk.tags.append(f"need:{target_ref.tag}")
                if seller_horizon:
                    sk.tags.append(f"seller_horizon:{seller_horizon}")
                rt = _best_need_tag(seller_need_map, p)
                if rt:
                    sk.tags.append(f"return_need:{rt}")
                skeletons.append(sk)

    # --- archetype: salary match focus (win-now sellers tend to want immediate contributors)
    salary_pool = match if win_nowish else filler
    for p in _sample_for_counterparty(salary_pool, target.salary_m, need_map=seller_need_map, rng=rng, k=salary_k):
        sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
        sk.seller_players_out = [target.player_id]
        sk.buyer_players_out = [p.player_id]
        sk.tags.append("archetype:salary_match")
        sk.tags.append(f"need:{target_ref.tag}")
        if seller_horizon:
            sk.tags.append(f"seller_horizon:{seller_horizon}")
        rt = _best_need_tag(seller_need_map, p)
        if rt:
            sk.tags.append(f"return_need:{rt}")
        skeletons.append(sk)

    # --- archetype: consolidate (2-for-1) (mostly a win-now depth play; disabled for 2nd apron hint)
    if enable_2for1 and cons and filler:
        top_a = _rank_for_need(cons[:6], need_map=seller_need_map)[:2]
        top_b = _rank_for_need(filler[:10], need_map=seller_need_map)[:4]
        for a in top_a:
            for b in top_b:
                if a.player_id == b.player_id:
                    continue
                if a.aggregation_solo_only or b.aggregation_solo_only:
                    continue
                sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
                sk.seller_players_out = [target.player_id]
                sk.buyer_players_out = [a.player_id, b.player_id]
                sk.tags.append("archetype:2for1")
                sk.tags.append(f"need:{target_ref.tag}")
                if seller_horizon:
                    sk.tags.append(f"seller_horizon:{seller_horizon}")
                rta = _best_need_tag(seller_need_map, a)
                rtb = _best_need_tag(seller_need_map, b)
                if rta:
                    sk.tags.append(f"return_need:{rta}")
                if rtb and rtb != rta:
                    sk.tags.append(f"return_need:{rtb}")
                skeletons.append(sk)

    # shuffle + keep only a small bounded set per target
    rng.shuffle(skeletons)
    return skeletons[: max(6, min(16, 2 * state.cfg.beam_width))]


def _buyer_can_absorb_target(tick_ctx: TradeGenerationTickContext, buyer_id: str, target_salary_m: float) -> bool:
    ts = tick_ctx.get_team_situation(buyer_id)
    cap_space = 0.0
    try:
        cap_space = float(getattr(getattr(ts, "constraints", None), "cap_space", 0.0) or 0.0)
    except Exception:
        cap_space = 0.0
    # cap_space is in dollars, target_salary_m is in millions
    return cap_space >= float(target_salary_m) * 1_000_000.0 * 1.02


def _collect_buyer_player_candidates(state: _GenState, buyer_out: TeamOutgoingCatalog, *, receiver_team_id: Optional[str] = None) -> Dict[str, List[PlayerTradeCandidate]]:
    """Bucket buyer outgoing players into candidate sets for archetypes."""
    cfg = state.cfg
    tick_ctx = state.tick_ctx
    buyer_id = buyer_out.team_id
    buyer_ts = tick_ctx.get_team_situation(buyer_id)
    posture = str(getattr(buyer_ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()


    receiver_id = _canon_team_id(receiver_team_id or '') if receiver_team_id else ''

    # gather players excluding banned/locked
    all_players: List[PlayerTradeCandidate] = []
    for pid, cand in buyer_out.players.items():
        if pid in state.banned_players[buyer_id]:
            continue
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            continue
        if _is_ban_active(tick_ctx.current_date, cand.recent_signing_banned_until):
            continue
        if receiver_id:
            # Block sending this player to receiver_id if SSOT return-bans or learned bans apply.
            if receiver_id in set(cand.return_ban_teams or ()):  # type: ignore[arg-type]
                continue
            if receiver_id in state.banned_receivers_by_player.get(pid, set()):
                continue
        all_players.append(cand)

    # classify by buckets
    filler_buckets = ("FILLER_CHEAP", "EXPIRING", "SURPLUS_LOW_FIT", "SURPLUS_REDUNDANT", "VETERAN_SALE", "FILLER_BAD_CONTRACT")
    match_buckets = ("EXPIRING", "SURPLUS_REDUNDANT", "CONSOLIDATE", "SURPLUS_LOW_FIT", "FILLER_CHEAP")
    consolidate_buckets = ("CONSOLIDATE", "SURPLUS_REDUNDANT", "SURPLUS_LOW_FIT")

    def is_core(c: PlayerTradeCandidate) -> bool:
        return "CORE" in (c.buckets or ())

    # filler: low-importance, non-core
    filler = [c for c in all_players if (not is_core(c)) and any(b in (c.buckets or ()) for b in filler_buckets)]
    # match: closer to salary, still non-core
    match = [c for c in all_players if (not is_core(c)) and any(b in (c.buckets or ()) for b in match_buckets)]
    # consolidate: higher quality non-core
    consolidate = [c for c in all_players if (not is_core(c)) and any(b in (c.buckets or ()) for b in consolidate_buckets)]

    # young: derived (age-based)
    young = []
    for c in all_players:
        if is_core(c):
            continue
        age = getattr(c.snap, "age", None)
        try:
            age_f = float(age) if age is not None else None
        except Exception:
            age_f = None
        if age_f is None:
            continue
        if age_f <= 25.0 and float(getattr(c.market, "total", 0.0) or 0.0) <= 22.0:
            young.append(c)

    # Sort
    filler.sort(key=lambda c: (float(getattr(c.market, "total", 0.0) or 0.0), float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))
    # match: keep salary diversity; do NOT bias to an arbitrary salary anchor (e.g. $10M)
    match.sort(key=lambda c: (-float(getattr(c.market, "total", 0.0) or 0.0), -float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))
    consolidate.sort(key=lambda c: (-float(getattr(c.market, "total", 0.0) or 0.0), -float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))
    young.sort(key=lambda c: (-float(getattr(c.market, "total", 0.0) or 0.0), float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))

    # In BUY posture, be less willing to ship out high-value consolidate pieces
    if posture in ("AGGRESSIVE_BUY", "SOFT_BUY"):
        consolidate = consolidate[:4]

    return {"filler": filler[:14], "match": match[:28], "young": young[:6], "consolidate": consolidate[:8]}


def _sample_near_salary(cands: Sequence[PlayerTradeCandidate], target_salary_m: float, *, rng: random.Random, k: int) -> List[PlayerTradeCandidate]:
    """Sample up to k candidates with salary close to target."""
    rows = []
    for c in cands:
        try:
            s = float(getattr(c, "salary_m", 0.0) or 0.0)
        except Exception:
            s = 0.0
        rows.append((abs(s - float(target_salary_m)), -float(getattr(c.market, "total", 0.0) or 0.0), c.player_id, c))
    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    top = [r[3] for r in rows[: max(2, min(8, len(rows)))]]
    rng.shuffle(top)
    return top[: max(0, k)]


def _need_fit_score(need_map: Mapping[str, float], cand: PlayerTradeCandidate) -> float:
    """How well a candidate matches a team's needs (0..~)."""
    if not need_map:
        return 0.0
    supply = getattr(cand, "supply", {}) or {}
    tags = getattr(cand, "top_tags", ()) or ()
    score = 0.0
    for t in tags:
        try:
            w = float(need_map.get(t, 0.0) or 0.0)
            s = float(supply.get(t, 0.0) or 0.0)
        except Exception:
            continue
        score += w * (0.4 + 0.6 * s)
    return float(score)


def _best_need_tag(need_map: Mapping[str, float], cand: PlayerTradeCandidate) -> str:
    """Return the best-matching need tag for narrative tags (or empty)."""
    if not need_map:
        return ""
    supply = getattr(cand, "supply", {}) or {}
    tags = getattr(cand, "top_tags", ()) or ()
    best_t = ""
    best = 0.0
    for t in tags:
        try:
            w = float(need_map.get(t, 0.0) or 0.0)
            s = float(supply.get(t, 0.0) or 0.0)
            sc = w * (0.4 + 0.6 * s)
        except Exception:
            continue
        if sc > best:
            best = sc
            best_t = str(t)
    return best_t if best > 0.05 else ""


def _rank_for_need(cands: Sequence[PlayerTradeCandidate], *, need_map: Mapping[str, float]) -> List[PlayerTradeCandidate]:
    """Deterministic ranking of candidates by need fit (then by market value, then salary)."""
    rows = []
    for c in cands:
        nf = _need_fit_score(need_map, c)
        mv = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        sal = float(getattr(c, "salary_m", 0.0) or 0.0)
        rows.append((nf, mv, sal, c.player_id, c))
    rows.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    return [r[-1] for r in rows]


def _sample_for_counterparty(
    cands: Sequence[PlayerTradeCandidate],
    target_salary_m: float,
    *,
    need_map: Mapping[str, float],
    rng: random.Random,
    k: int,
) -> List[PlayerTradeCandidate]:
    """Sample candidates with a blend of salary proximity and need fit.

    This is purely a heuristic for *plausible* packages; SSOT evaluation decides acceptance later.
    """
    rows = []
    for c in cands:
        try:
            sal = float(getattr(c, "salary_m", 0.0) or 0.0)
        except Exception:
            sal = 0.0
        mv = float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        nf = _need_fit_score(need_map, c)
        dist = abs(sal - float(target_salary_m))
        # Higher is better: need fit dominates slightly; salary distance keeps things plausible.
        score = (1.45 * nf) - (0.14 * dist) - (0.015 * max(0.0, mv - 18.0))
        rows.append((score, -nf, dist, mv, c.player_id, c))

    rows.sort(key=lambda x: (x[0], x[1], -x[2], x[4]), reverse=True)
    top = [r[-1] for r in rows[: max(2, min(10, len(rows)))]]
    rng.shuffle(top)
    return top[: max(0, k)]


def _picks_packages(state: _GenState, buyer_out: TeamOutgoingCatalog, *, max_packages: int, prefer_second: bool = False) -> List[Tuple[Tuple[str, ...], Tuple[str, ...], str]]:
    """Build a few pick/swap packages for the buyer.

    prefer_second=True biases toward SECOND-based packages and uses FIRST picks only as fallback.

    NOTE: We apply a lightweight Stepien precheck here to avoid generating obviously
    invalid first-pick combinations and wasting validation budget.
    """
    team_id = buyer_out.team_id
    picks_second = [pid for pid in buyer_out.pick_ids_by_bucket.get("SECOND", ()) if pid not in state.banned_picks[team_id]]
    picks_first_safe = [pid for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SAFE", ()) if pid not in state.banned_picks[team_id]]
    picks_first_sens = [pid for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SENSITIVE", ()) if pid not in state.banned_picks[team_id]]
    swaps = [sid for sid in buyer_out.swap_ids if sid not in state.banned_swaps[team_id]]

    seconds_pkgs: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
    swap_pkgs: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
    first_pkgs: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []

    # seconds-first packages
    if picks_second:
        seconds_pkgs.append(((picks_second[0],), tuple(), "sweetener:2RP"))
    if len(picks_second) >= 2:
        seconds_pkgs.append(((picks_second[0], picks_second[1]), tuple(), "sweetener:2RPx2"))
    if picks_second and swaps:
        seconds_pkgs.append(((picks_second[0],), (swaps[0],), "sweetener:2RP+swap"))

    # swaps (cheap but valuable)
    if swaps:
        swap_pkgs.append((tuple(), (swaps[0],), "sweetener:swap"))

    # first-round picks as fallback
    if picks_first_safe:
        first_pkgs.append(((picks_first_safe[0],), tuple(), "sweetener:1RP_SAFE"))
    if picks_first_sens:
        first_pkgs.append(((picks_first_sens[0],), tuple(), "sweetener:1RP_SENSITIVE"))

    ordered: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]]
    if prefer_second:
        ordered = seconds_pkgs + swap_pkgs + first_pkgs
    else:
        # stable cheap->expensive order (legacy)
        ordered = []
        ordered.extend(seconds_pkgs[:2])
        ordered.extend(swap_pkgs[:1])
        ordered.extend(first_pkgs)

    # Stepien precheck (best-effort): only checks the outgoing picks in the package itself.
    stepien = getattr(state.catalog, "stepien", None)
    filtered: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
    for picks_out, swaps_out, tag in ordered:
        if picks_out and stepien is not None:
            if not _stepien_ok_after(stepien, team_id, outgoing_pick_ids=set(picks_out)):
                state.stats.stepien_precheck_blocked += 1
                continue
        filtered.append((picks_out, swaps_out, tag))
        if len(filtered) >= max(0, int(max_packages)):
            break

    return filtered


# =============================================================================
# Validate + repair
# =============================================================================
def _repair_until_valid(state: _GenState, spec: _DealSpec, *, budgets: _Budgets) -> Optional[Deal]:
    cfg = state.cfg
    tick_ctx = state.tick_ctx

    current = spec.copy()

    for attempt in range(max(1, budgets.max_repairs + 1)):
        deal = _spec_to_deal(state, current)
        if deal is None:
            return None

        # complexity guard (early)
        if _deal_complexity_exceeds(cfg, deal):
            return None

        try:
            tick_ctx.validate_deal(deal, allow_locked_by_deal_id=state.allow_locked_by_deal_id)
            state.stats.validations += 1
            return deal
        except TradeError as err:
            state.stats.validations += 1
            state.stats.record_error(err)

            # fatal / non-repairable errors (ban and prune)
            if err.code in (ASSET_LOCKED,):
                state.stats.pruned_locked += 1
                _ban_from_error(state, err)
                return None
            if err.code in (PLAYER_NOT_OWNED, PICK_NOT_OWNED, SWAP_NOT_OWNED, SWAP_NOT_FOUND, SWAP_INVALID, DUPLICATE_ASSET):
                _ban_from_error(state, err)
                return None

            # roster limit: remove lowest-impact filler from receiver side
            if err.code == ROSTER_LIMIT:
                if not _repair_roster_limit(state, current, err):
                    return None
                continue

            # deal invalidated: inspect rule/method/reason
            details = err.details if isinstance(err.details, dict) else {}
            rule = str(details.get("rule") or "")
            reason = str(details.get("reason") or "")
            method = str(details.get("method") or "")
            team_id = _canon_team_id(details.get("team_id") or "")

            if rule == "salary_matching":
                if method == "second_apron_one_for_one":
                    if not _repair_second_apron_one_for_one(state, current, team_id=team_id):
                        return None
                    continue
                if not _repair_salary_matching(state, current, details):
                    return None
                continue

            if rule in ("player_eligibility", "return_to_trading_team_same_season"):
                state.stats.pruned_ineligible += 1
                _ban_from_error(state, err)
                return None

            if rule == "pick_rules":
                if reason in ("stepien_violation", "pick_too_far"):
                    state.stats.pruned_stepien += 1
                    if not _repair_pick_rules(state, current, details):
                        _ban_from_error(state, err)
                        return None
                    continue
                # other pick rule issues: prune
                _ban_from_error(state, err)
                return None

            # unknown invalidation => prune
            return None

    return None


def _deal_complexity_exceeds(cfg: DealGeneratorConfig, deal: Deal) -> bool:
    n_assets = sum(len(v) for v in (deal.legs or {}).values())
    n_players = 0
    for assets in (deal.legs or {}).values():
        for a in assets:
            if isinstance(a, PlayerAsset):
                n_players += 1
    return (n_assets > cfg.max_assets) or (n_players > cfg.max_players_moved)


def _is_locked(lock: Any, *, allow_locked_by_deal_id: Optional[str]) -> bool:
    if not lock:
        return False
    try:
        if not bool(getattr(lock, "is_locked", False)):
            return False
    except Exception:
        return False
    deal_id = getattr(lock, "deal_id", None)
    if allow_locked_by_deal_id and deal_id and str(deal_id) == str(allow_locked_by_deal_id):
        return False
    return True


def _ban_from_error(state: _GenState, err: TradeError) -> None:
    details = err.details if isinstance(err.details, dict) else {}
    rule = str(details.get("rule") or "")
    if rule == "return_to_trading_team_same_season":
        pid = details.get("player_id")
        to_team = _canon_team_id(details.get("to_team") or "")
        if pid and to_team:
            state.banned_receivers_by_player[str(pid)].add(to_team)
    # best-effort: ban offending asset id
    pid = details.get("player_id")
    if pid:
        team_id = _canon_team_id(details.get("team_id") or "")
        if team_id:
            state.banned_players[team_id].add(str(pid))
    pick_id = details.get("pick_id")
    if pick_id:
        team_id = _canon_team_id(details.get("team_id") or "")
        if team_id:
            state.banned_picks[team_id].add(str(pick_id))
    swap_id = details.get("swap_id")
    if swap_id:
        team_id = _canon_team_id(details.get("team_id") or "")
        if team_id:
            state.banned_swaps[team_id].add(str(swap_id))


def _repair_roster_limit(state: _GenState, spec: _DealSpec, err: TradeError) -> bool:
    details = err.details if isinstance(err.details, dict) else {}
    team_id = _canon_team_id(details.get("team_id") or "")
    if not team_id:
        return False

    # count is the post-trade roster size for the failing team
    try:
        count = int(details.get("count") or 0)
    except Exception:
        count = 0

    buyer_id = spec.buyer_id
    seller_id = spec.seller_id
    catalog = state.catalog

    # (1) Prefer reducing incoming players for the violating team when possible.
    # This keeps the deal structure simple (especially for 2-for-1 archetypes).
    if team_id == buyer_id and len(spec.seller_players_out) > 1:
        spec.seller_players_out = spec.seller_players_out[:1]
        spec.tags.append("repair:roster_trim_seller")
        return True

    if team_id == seller_id and len(spec.buyer_players_out) > 1:
        spec.buyer_players_out = spec.buyer_players_out[:1]
        spec.tags.append("repair:roster_trim_buyer")
        return True

    # (2) Common case: team is already at 15 and receives 1 player (new_count == 16).
    # Repair by having the violating team send out a low-value filler to make room.
    if team_id == buyer_id:
        buyer_out = catalog.outgoing_by_team.get(buyer_id)
        if buyer_out is None:
            return False

        # Don't create multi-player outgoing if any current outgoing is aggregation solo-only.
        if any(buyer_out.players.get(pid) and buyer_out.players[pid].aggregation_solo_only for pid in spec.buyer_players_out):
            return False

        # If count isn't available, still attempt at most one send-out.
        need_send = 1 if count <= 0 else max(0, count - 15)
        if need_send <= 0:
            need_send = 1
        need_send = min(need_send, 1)

        filler_cands = _collect_buyer_player_candidates(state, buyer_out, receiver_team_id=seller_id)["filler"]
        used = set(spec.buyer_players_out)
        allow_solo_only = len(spec.buyer_players_out) == 0
        for c in filler_cands:
            if c.player_id in used:
                continue
            if c.aggregation_solo_only and not allow_solo_only:
                continue
            spec.buyer_players_out.append(c.player_id)
            spec.tags.append("repair:roster_send_filler_buyer")
            return True

        return False

    if team_id == seller_id:
        # Rare in our generator (mostly triggered by 2-for-1 offers).
        # If trimming didn't help, attempt to have seller send out one extra low-value player.
        seller_out = catalog.outgoing_by_team.get(seller_id)
        if seller_out is None:
            return False

        if any(seller_out.players.get(pid) and seller_out.players[pid].aggregation_solo_only for pid in spec.seller_players_out):
            return False

        need_send = 1 if count <= 0 else max(0, count - 15)
        if need_send <= 0:
            need_send = 1
        need_send = min(need_send, 1)

        filler_cands = _collect_buyer_player_candidates(state, seller_out, receiver_team_id=buyer_id)["filler"]
        used = set(spec.seller_players_out)
        allow_solo_only = len(spec.seller_players_out) == 0
        for c in filler_cands:
            if c.player_id in used:
                continue
            if c.aggregation_solo_only and not allow_solo_only:
                continue
            spec.seller_players_out.append(c.player_id)
            spec.tags.append("repair:roster_send_filler_seller")
            return True

        return False

    return False


def _repair_second_apron_one_for_one(state: _GenState, spec: _DealSpec, *, team_id: str) -> bool:
    """Repair for SECOND_APRON one-for-one restriction.

    SalaryMatchingRule enforces that a SECOND_APRON team cannot trade if it would have
    outgoing_players > 1 OR incoming_players > 1.

    In a 2-team deal, that implies BOTH sides must be capped at 1 player-out, because:
      - incoming_players for one team == other team's outgoing players (players assets)
    We only trim lists that actually exceed 1.
    """
    tid = _canon_team_id(team_id or "")
    changed = False

    # For 2-team deals, satisfying the apron team's incoming/outgoing constraints
    # requires both lists to be <= 1. Keep the "primary" player on each side.
    if len(spec.seller_players_out) > 1:
        spec.seller_players_out = spec.seller_players_out[:1]
        changed = True
    if len(spec.buyer_players_out) > 1:
        spec.buyer_players_out = spec.buyer_players_out[:1]
        changed = True

    if changed:
        spec.tags.append(f"repair:second_apron_1for1:{tid or 'unknown'}")
    return changed


def _repair_salary_matching(state: _GenState, spec: _DealSpec, details: Dict[str, Any]) -> bool:
    """Meta-driven salary-match repair (bounded).

    Uses SalaryMatchingRule details:
      - team_id, status, outgoing_salary, incoming_salary, allowed_in, method
    Strategy:
      - If failing team needs MORE outgoing salary: add the cheapest-possible salary filler close to the deficit.
        If SECOND_APRON, prefer swapping to a higher-salary single outgoing instead of adding a 2nd player.
      - If failing team needs LESS incoming salary: trim extra incoming players first, then swap to a cheaper player.
    """
    team_id = _canon_team_id(details.get("team_id") or "")
    if not team_id:
        return False

    buyer_id = spec.buyer_id
    seller_id = spec.seller_id
    catalog = state.catalog

    buyer_out = catalog.outgoing_by_team.get(buyer_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if buyer_out is None or seller_out is None:
        return False

    # Pull numeric details (dollars)
    try:
        incoming_salary = float(details.get("incoming_salary") or 0.0)
    except Exception:
        incoming_salary = 0.0
    try:
        outgoing_salary = float(details.get("outgoing_salary") or 0.0)
    except Exception:
        outgoing_salary = 0.0
    try:
        allowed_in = float(details.get("allowed_in") or 0.0)
    except Exception:
        allowed_in = 0.0

    status = str(details.get("status") or "")
    method = str(details.get("method") or "")

    def _salary_m(c: PlayerTradeCandidate) -> float:
        try:
            return float(getattr(c, "salary_m", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _value(c: PlayerTradeCandidate) -> float:
        try:
            return float(getattr(getattr(c, "market", None), "total", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _pool_for(out_cat: TeamOutgoingCatalog, receiver_team_id: str) -> List[PlayerTradeCandidate]:
        packs = _collect_buyer_player_candidates(state, out_cat, receiver_team_id=receiver_team_id)
        filler = list(packs.get("filler") or [])
        match = list(packs.get("match") or [])
        seen: Set[str] = set()
        pool: List[PlayerTradeCandidate] = []
        for c in filler + match:
            if not c or not getattr(c, "player_id", None):
                continue
            if c.player_id in seen:
                continue
            seen.add(c.player_id)
            pool.append(c)
        return pool

    # ---------------------------------------------------------------------
    # Case A: buyer fails (buyer incoming salary too high vs allowed_in) => increase buyer outgoing salary.
    # ---------------------------------------------------------------------
    if team_id == buyer_id:
        # If we're at SECOND_APRON, we cannot add a 2nd outgoing player once we already have one.
        second_apron_one_for_one = (status == "SECOND_APRON") or (method == "outgoing_second_apron")

        # aggregation_solo_only means the team cannot aggregate multiple outgoing players;
        # it can still trade a single player (including another solo-only player).
        has_solo_only_outgoing = any(
            buyer_out.players.get(pid) and buyer_out.players[pid].aggregation_solo_only
            for pid in spec.buyer_players_out
        )

        deficit_dollars = max(0.0, incoming_salary - allowed_in) if allowed_in > 0 else max(0.0, incoming_salary)
        needed_extra_m = deficit_dollars / 1_000_000.0

        pool = _pool_for(buyer_out, receiver_team_id=seller_id)
        used = set(spec.buyer_players_out)

        def pick_best_to_add() -> Optional[PlayerTradeCandidate]:
            best_c: Optional[PlayerTradeCandidate] = None
            best_score: Optional[float] = None
            for c in pool:
                if c.player_id in used:
                    continue
                if c.aggregation_solo_only and len(spec.buyer_players_out) >= 1:
                    continue
                sal = _salary_m(c)
                if sal <= 0:
                    continue
                val = _value(c)
                # Prefer covering most of the deficit with minimal value cost.
                score = abs(sal - needed_extra_m) + 0.03 * val
                if needed_extra_m > 0.75 and sal < 0.60 * needed_extra_m:
                    score += (0.60 * needed_extra_m - sal) * 2.5
                if best_score is None or score < best_score:
                    best_score = score
                    best_c = c
            return best_c

        def pick_best_swap_higher(current_pid: str) -> Optional[PlayerTradeCandidate]:
            cur = buyer_out.players.get(current_pid)
            cur_sal = _salary_m(cur) if cur is not None else 0.0
            min_sal = max(cur_sal + max(0.25, needed_extra_m), cur_sal + 0.25)

            best_c: Optional[PlayerTradeCandidate] = None
            best_score: Optional[float] = None
            for c in pool:
                if c.player_id == current_pid:
                    continue
                if c.player_id in used:
                    continue
                sal = _salary_m(c)
                if sal + 1e-6 < min_sal:
                    continue
                val = _value(c)
                over = sal - min_sal
                score = over + 0.03 * val
                if best_score is None or score < best_score:
                    best_score = score
                    best_c = c
            return best_c

        # Prefer add when allowed; otherwise swap to keep 1 outgoing player.
        can_add_outgoing = (
            not (second_apron_one_for_one and len(spec.buyer_players_out) >= 1)
            and not (has_solo_only_outgoing and len(spec.buyer_players_out) >= 1)
        )
        if can_add_outgoing:
            cand = pick_best_to_add()
            if cand is not None:
                spec.buyer_players_out.append(cand.player_id)
                spec.tags.append("repair:add_salary_filler_buyer")
                return True

        # Fallback: swap a single outgoing to a higher-salary alternative.
        if len(spec.buyer_players_out) == 1:
            cur_pid = spec.buyer_players_out[0]
            cand = pick_best_swap_higher(cur_pid)
            if cand is not None:
                spec.buyer_players_out[0] = cand.player_id
                spec.tags.append("repair:swap_higher_salary_buyer")
                return True

        # If we had 0 outgoing players (picks-only), adding one is still allowed even under SECOND_APRON.
        if len(spec.buyer_players_out) == 0:
            cand = pick_best_to_add()
            if cand is not None:
                spec.buyer_players_out.append(cand.player_id)
                spec.tags.append("repair:add_outgoing_required_buyer")
                return True

        return False

    # ---------------------------------------------------------------------
    # Case B: seller fails (seller incoming salary too high vs allowed_in) => reduce buyer outgoing salary.
    # ---------------------------------------------------------------------
    if team_id == seller_id:
        # Trim extra incoming players first (common for 2-for-1).
        if len(spec.buyer_players_out) >= 2:
            spec.buyer_players_out = spec.buyer_players_out[:1]
            spec.tags.append("repair:trim_incoming_seller")
            return True

        if len(spec.buyer_players_out) != 1:
            return False

        # Replace buyer outgoing player with a cheaper one that fits seller's allowed_in.
        allowed_max_m = allowed_in / 1_000_000.0
        if allowed_max_m <= 0.0:
            return False

        cur_pid = spec.buyer_players_out[0]
        pool = _pool_for(buyer_out, receiver_team_id=seller_id)

        candidates = []
        for c in pool:
            if c.player_id == cur_pid:
                continue
            sal = _salary_m(c)
            if sal <= 0:
                continue
            if sal - 1e-6 > allowed_max_m:
                continue
            candidates.append(c)

        if not candidates:
            return False

        candidates.sort(key=lambda c: (-_value(c), -_salary_m(c), c.player_id))
        spec.buyer_players_out[0] = candidates[0].player_id
        spec.tags.append("repair:swap_cheaper_buyer_for_seller")
        return True

    return False


def _repair_pick_rules(state: _GenState, spec: _DealSpec, details: Dict[str, Any]) -> bool:
    """Downgrade/remove picks to satisfy Stepien or pick horizon.

    IMPORTANT POLICY:
    - stepien_violation is typically a *combination* issue; do NOT hard-ban a pick_id.
      We only drop the last-added asset (soft drop) and let exploration try other combos.
    - pick_too_far (horizon) is effectively intrinsic for that pick, so hard-banning is OK.
    """
    reason = str(details.get("reason") or "")
    hard_ban = reason in ("pick_too_far",)

    # Simple strategy: remove the last-added pick/swap from the side indicated.
    team_id = _canon_team_id(details.get("team_id") or "")
    if not team_id:
        # If missing, assume buyer side (most common)
        team_id = spec.buyer_id

    def _drop_pick(team: str, *, seller_side: bool) -> bool:
        if team == spec.buyer_id:
            if spec.buyer_picks_out:
                removed = str(spec.buyer_picks_out.pop())
                if hard_ban:
                    state.banned_picks[spec.buyer_id].add(removed)
                else:
                    state.stats.stepien_soft_drops += 1
                spec.tags.append("repair:drop_pick" + ("_seller" if seller_side else ""))
                return True
            if spec.buyer_swaps_out:
                removed = str(spec.buyer_swaps_out.pop())
                if hard_ban:
                    state.banned_swaps[spec.buyer_id].add(removed)
                else:
                    # swaps rarely cause stepien_violation; still keep policy consistent
                    state.stats.stepien_soft_drops += 1
                spec.tags.append("repair:drop_swap" + ("_seller" if seller_side else ""))
                return True
        if team == spec.seller_id:
            if spec.seller_picks_out:
                removed = str(spec.seller_picks_out.pop())
                if hard_ban:
                    state.banned_picks[spec.seller_id].add(removed)
                else:
                    state.stats.stepien_soft_drops += 1
                spec.tags.append("repair:drop_pick_seller")
                return True
            if spec.seller_swaps_out:
                removed = str(spec.seller_swaps_out.pop())
                if hard_ban:
                    state.banned_swaps[spec.seller_id].add(removed)
                else:
                    state.stats.stepien_soft_drops += 1
                spec.tags.append("repair:drop_swap_seller")
                return True
        return False

    if team_id == spec.buyer_id:
        return _drop_pick(spec.buyer_id, seller_side=False)
    if team_id == spec.seller_id:
        return _drop_pick(spec.seller_id, seller_side=True)
    # Fallback
    return _drop_pick(spec.buyer_id, seller_side=False)


def _spec_to_deal(state: _GenState, spec: _DealSpec) -> Optional[Deal]:
    """Convert a spec to a 2-team Deal object, with strict asset validity checks."""
    catalog = state.catalog
    buyer_id = _canon_team_id(spec.buyer_id)
    seller_id = _canon_team_id(spec.seller_id)
    if not buyer_id or not seller_id or buyer_id == seller_id:
        return None

    buyer_out = catalog.outgoing_by_team.get(buyer_id)
    seller_out = catalog.outgoing_by_team.get(seller_id)
    if buyer_out is None or seller_out is None:
        return None

    # Build legs
    legs: Dict[str, List[Any]] = {buyer_id: [], seller_id: []}

    # buyer players
    for pid in spec.buyer_players_out:
        cand = buyer_out.players.get(pid)
        if cand is None:
            return None
        if pid in state.banned_players[buyer_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[buyer_id].append(cand.as_asset(to_team=None))

    # buyer picks
    for pick_id in spec.buyer_picks_out:
        cand = buyer_out.picks.get(pick_id)
        if cand is None:
            return None
        if pick_id in state.banned_picks[buyer_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[buyer_id].append(cand.as_asset(to_team=None))

    # buyer swaps
    for swap_id in spec.buyer_swaps_out:
        cand = buyer_out.swaps.get(swap_id)
        if cand is None:
            return None
        if swap_id in state.banned_swaps[buyer_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[buyer_id].append(cand.as_asset(to_team=None))

    # seller players
    for pid in spec.seller_players_out:
        cand = seller_out.players.get(pid)
        if cand is None:
            return None
        if pid in state.banned_players[seller_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        # eligibility prefilter (recent-signing ban is absolute; aggregation is handled by solo-only constraint)
        if _is_ban_active(state.tick_ctx.current_date, cand.recent_signing_banned_until):
            return None
        legs[seller_id].append(cand.as_asset(to_team=None))

    # seller picks/swaps rarely used in this generator, but supported
    for pick_id in spec.seller_picks_out:
        cand = seller_out.picks.get(pick_id)
        if cand is None:
            return None
        if pick_id in state.banned_picks[seller_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[seller_id].append(cand.as_asset(to_team=None))

    for swap_id in spec.seller_swaps_out:
        cand = seller_out.swaps.get(swap_id)
        if cand is None:
            return None
        if swap_id in state.banned_swaps[seller_id]:
            return None
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            return None
        legs[seller_id].append(cand.as_asset(to_team=None))

    # Always omit meta for dedupe stability (meta is orchestrator territory)
    meta = {"tags": list(dict.fromkeys(spec.tags))} if spec.tags else {}
    deal = Deal(teams=[buyer_id, seller_id], legs=legs, meta=meta)
    return deal


# =============================================================================
# Evaluate + scoring
# =============================================================================
def _evaluate_and_score(state: _GenState, deal: Deal, *, buyer_id: str, seller_id: str, partner_id: Optional[str] = None) -> Optional[DealProposal]:
    cfg = state.cfg
    tick_ctx = state.tick_ctx

    try:
        buyer_decision, buyer_eval = evaluate_deal_for_team(
            deal,
            buyer_id,
            tick_ctx=tick_ctx,
            include_breakdown=False,
            validate=False,
        )
        seller_decision, seller_eval = evaluate_deal_for_team(
            deal,
            seller_id,
            tick_ctx=tick_ctx,
            include_breakdown=False,
            validate=False,
        )
        state.stats.evaluations += 2
    except TradeError:
        # valuation should rarely raise TradeError when validate=False, but be defensive
        return None
    except Exception:
        return None

    score = _score_deal(cfg, buyer_decision, buyer_eval, seller_decision, seller_eval)
    tags = tuple(_extract_tags_from_deal(deal))

    return DealProposal(
        deal=deal,
        buyer_id=buyer_id,
        seller_id=seller_id,
        buyer_decision=buyer_decision,
        seller_decision=seller_decision,
        buyer_eval=buyer_eval,
        seller_eval=seller_eval,
        score=float(score),
        tags=tags,
    )


def _score_deal(cfg: DealGeneratorConfig, bd: DealDecision, be: TeamDealEvaluation, sd: DealDecision, se: TeamDealEvaluation) -> float:
    mb = float(getattr(be, "net_surplus", 0.0) or 0.0) - float(getattr(bd, "required_surplus", 0.0) or 0.0)
    ms = float(getattr(se, "net_surplus", 0.0) or 0.0) - float(getattr(sd, "required_surplus", 0.0) or 0.0)

    # accept score uses sigmoid on margins
    scale = float(cfg.sigmoid_scale or 3.5)
    accept = _sigmoid(mb / scale) + _sigmoid(ms / scale)

    # complexity
    num_assets = int(_count_assets(be, se))
    num_players = int(_count_players(be, se))
    complexity = cfg.complexity_penalty_assets * max(0, num_assets - 2) + cfg.complexity_penalty_players * max(0, num_players - 2)

    # buyer overpay penalty (discourage buyer losing badly)
    overpay = max(0.0, -mb) * float(cfg.buyer_overpay_penalty)

    return float(accept - complexity - overpay)


def _count_assets(be: TeamDealEvaluation, se: TeamDealEvaluation) -> int:
    # total moved assets is incoming+outgoing across both sides, but those lists are symmetric.
    try:
        return int(len(getattr(be.side, "incoming", ())) + len(getattr(be.side, "outgoing", ())))
    except Exception:
        return 0


def _count_players(be: TeamDealEvaluation, se: TeamDealEvaluation) -> int:
    def _is_player(tv) -> bool:
        k = getattr(tv, "kind", None)
        if k is None:
            return False
        # AssetKind Enum이면 .value가 "player" 형태. (str(Enum)은 "AssetKind.PLAYER"가 될 수 있음)
        v = getattr(k, "value", k)
        return str(v).strip().lower() == "player"

    def _n_players(side):
        n = 0
        for tv in getattr(side, "incoming", ()):
            if _is_player(tv):
                n += 1
        for tv in getattr(side, "outgoing", ()):
            if _is_player(tv):
                n += 1
        return n
    try:
        return int(_n_players(be.side))
    except Exception:
        return 0


def _sigmoid(x: float) -> float:
    # safe sigmoid
    try:
        if x >= 0:
            z = math.exp(-x)
            return 1.0 / (1.0 + z)
        z = math.exp(x)
        return z / (1.0 + z)
    except Exception:
        return 0.5


def _extract_tags_from_deal(deal: Deal) -> List[str]:
    tags: List[str] = []
    meta = getattr(deal, "meta", None)
    if isinstance(meta, dict):
        t = meta.get("tags")
        if isinstance(t, list):
            tags.extend([str(x) for x in t if x])
    return tags


# =============================================================================
# Sweetener loop (minimal counter-ish)
# =============================================================================
def _sweetener_loop(state: _GenState, proposal: DealProposal, *, budgets: _Budgets, partner_id: Optional[str] = None) -> List[DealProposal]:
    """Try adding small sweeteners to salvage a near-miss.

    Fixes:
    - Transactional: trial spec only; commit only on success.
    - Limit is on *committed* sweeteners (not attempts).
    - Avoid pick_rules repair that would silently drop the sweetener and still return a valid deal.
    """
    cfg = state.cfg
    buyer_id = proposal.buyer_id
    seller_id = proposal.seller_id

    def _margin(p: DealProposal) -> float:
        return float(getattr(p.seller_eval, "net_surplus", 0.0) or 0.0) - float(getattr(p.seller_decision, "required_surplus", 0.0) or 0.0)

    seller_margin = _margin(proposal)
    if seller_margin >= 0:
        return [proposal]
    if seller_margin < -float(cfg.near_miss_margin_max):
        return [proposal]

    # If they already accept, nothing to do.
    if getattr(proposal.seller_decision, "verdict", None) == DealVerdict.ACCEPT:
        return [proposal]

    buyer_out = state.catalog.outgoing_by_team.get(buyer_id)
    if buyer_out is None:
        return [proposal]

    stepien = state.catalog.stepien

    new_props: List[DealProposal] = [proposal]
    current_best = proposal
    current_spec = _deal_to_spec_guess(current_best.deal, buyer_id=buyer_id, seller_id=seller_id)
    if current_spec is None:
        return [proposal]

    max_add = max(0, int(cfg.max_sweeteners))
    if max_add <= 0:
        return [proposal]

    # Track already-added 2RPs (limit at 2).
    second_ids = set(buyer_out.pick_ids_by_bucket.get("SECOND", ()))

    def _count_seconds(spec: _DealSpec) -> int:
        return sum(1 for pid in spec.buyer_picks_out if pid in second_ids)

    committed = 0

    verdict_rank = {DealVerdict.REJECT: 0, DealVerdict.COUNTER: 1, DealVerdict.ACCEPT: 2}

    for token in cfg.sweetener_order:
        if committed >= max_add:
            break
        if state.stats.validations >= budgets.max_validations or state.stats.evaluations >= budgets.max_evaluations:
            break

        # Candidates: keep it cheap (try first viable only) to avoid increasing budgets.
        cand_pick: Optional[str] = None
        cand_swap: Optional[str] = None

        used_picks = set(current_spec.buyer_picks_out)
        used_swaps = set(current_spec.buyer_swaps_out)
        seconds_added = _count_seconds(current_spec)

        if token == "SECOND":
            if seconds_added >= 2:
                continue
            for pid in buyer_out.pick_ids_by_bucket.get("SECOND", ()):
                if pid in used_picks or pid in state.banned_picks[buyer_id]:
                    continue
                if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                    continue
                cand_pick = pid
                break

        elif token == "FIRST_SAFE":
            for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SAFE", ()):
                if pid in used_picks or pid in state.banned_picks[buyer_id]:
                    continue
                if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                    continue
                cand_pick = pid
                break

        elif token == "FIRST_SENSITIVE":
            for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SENSITIVE", ()):
                if pid in used_picks or pid in state.banned_picks[buyer_id]:
                    continue
                if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                    continue
                cand_pick = pid
                break

        elif token == "SWAP":
            for sid in buyer_out.swap_ids:
                if sid in used_swaps or sid in state.banned_swaps[buyer_id]:
                    continue
                cand_swap = sid
                break

        if cand_pick is None and cand_swap is None:
            continue

        # Trial (transactional)
        trial_spec = current_spec.copy()
        if cand_pick is not None:
            trial_spec.buyer_picks_out.append(cand_pick)
            trial_spec.tags.append({
                "SECOND": "sweetener:2RP",
                "FIRST_SAFE": "sweetener:1RP_SAFE",
                "FIRST_SENSITIVE": "sweetener:1RP_SENSITIVE",
            }.get(token, "sweetener:pick"))
        if cand_swap is not None:
            trial_spec.buyer_swaps_out.append(cand_swap)
            trial_spec.tags.append("sweetener:swap")

        # Validate without repair (sweetener must remain attached).
        state.stats.sweetener_trials += 1
        deal2 = _spec_to_deal(state, trial_spec)
        if deal2 is None or _deal_complexity_exceeds(cfg, deal2):
            state.stats.sweetener_rollbacks += 1
            continue

        try:
            state.tick_ctx.validate_deal(deal2, allow_locked_by_deal_id=state.allow_locked_by_deal_id)
            state.stats.validations += 1
        except TradeError as err:
            state.stats.validations += 1
            state.stats.record_error(err)
            # If this is an intrinsic pick horizon issue, ban the candidate pick.
            details = err.details if isinstance(err.details, dict) else {}
            if str(details.get("rule") or "") == "pick_rules" and str(details.get("reason") or "") == "pick_too_far":
                if cand_pick is not None:
                    state.banned_picks[buyer_id].add(str(cand_pick))
                    state.stats.pruned_stepien += 1
            state.stats.sweetener_rollbacks += 1
            continue

        fp = _deal_fingerprint_2team(deal2)
        if fp in state.seen_fingerprints:
            state.stats.pruned_duplicate += 1
            state.stats.sweetener_rollbacks += 1
            continue
        state.seen_fingerprints.add(fp)

        p2 = _evaluate_and_score(state, deal2, buyer_id=buyer_id, seller_id=seller_id, partner_id=partner_id or seller_id)
        if p2 is None:
            state.stats.sweetener_rollbacks += 1
            continue
        new_props.append(p2)

        # Commit only if it improves seller outcome (verdict or margin).
        old_v = getattr(current_best.seller_decision, "verdict", DealVerdict.REJECT)
        new_v = getattr(p2.seller_decision, "verdict", DealVerdict.REJECT)
        improve = verdict_rank.get(new_v, 0) > verdict_rank.get(old_v, 0) or (_margin(p2) > _margin(current_best) + 1e-6)
        if improve:
            current_best = p2
            current_spec = trial_spec
            committed += 1
            state.stats.sweetener_commits += 1
            state.stats.sweetener_commit_by_token[str(token)] += 1

        # Stop early if both accept.
        if getattr(p2.seller_decision, "verdict", None) == DealVerdict.ACCEPT and getattr(p2.buyer_decision, "verdict", None) == DealVerdict.ACCEPT:
            break

    new_props.sort(key=lambda x: x.score, reverse=True)
    return new_props[: max(1, min(3, budgets.beam_width))]


def _stepien_ok_after(stepien: StepienHelper, team_id: str, *, outgoing_pick_ids: Set[str]) -> bool:
    """Fast Stepien pre-check using StepienHelper (best-effort).

    We only need to know if the team remains Stepien-compliant after trading away
    the specified outgoing picks. Incoming picks do not affect compliance for the
    outgoing team (ownership is irrelevant), but StepienHelper requires both sets.
    """
    try:
        return bool(stepien.is_compliant_after(team_id=team_id, outgoing_pick_ids=set(outgoing_pick_ids), incoming_pick_ids=set()))
    except Exception:
        # If helper fails, allow validator to catch later.
        return True




def _deal_to_spec_guess(deal: Deal, *, buyer_id: str, seller_id: str) -> Optional[_DealSpec]:
    """Reconstruct a minimal spec from a deal (2-team only)."""
    buyer_id = _canon_team_id(buyer_id)
    seller_id = _canon_team_id(seller_id)
    if not buyer_id or not seller_id:
        return None
    legs = deal.legs or {}
    if buyer_id not in legs or seller_id not in legs:
        return None
    spec = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
    for a in legs.get(buyer_id, []):
        if isinstance(a, PlayerAsset):
            spec.buyer_players_out.append(a.player_id)
        elif isinstance(a, PickAsset):
            spec.buyer_picks_out.append(a.pick_id)
        elif isinstance(a, SwapAsset):
            spec.buyer_swaps_out.append(a.swap_id)
    for a in legs.get(seller_id, []):
        if isinstance(a, PlayerAsset):
            spec.seller_players_out.append(a.player_id)
        elif isinstance(a, PickAsset):
            spec.seller_picks_out.append(a.pick_id)
        elif isinstance(a, SwapAsset):
            spec.seller_swaps_out.append(a.swap_id)
    return spec


def _apply_partner_cap(
    state: _GenState,
    proposals: List[DealProposal],
    *,
    max_results: int,
    partner_side: str,
) -> List[DealProposal]:
    """Diversify final output by capping number of proposals per partner team.

    partner_side:
      - 'seller': cap by proposal.seller_id (BUY mode)
      - 'buyer':  cap by proposal.buyer_id  (SELL mode)

    Also applies a soft penalty (partner_repeat_penalty) during selection so repeated
    partners are less likely to crowd out variety *before* hitting the hard cap.
    """
    max_results_i = max(0, int(max_results))
    if max_results_i <= 0:
        return []

    cap = int(state.cfg.max_partner_repeats or 0)
    if cap <= 0:
        return proposals[:max_results_i]

    penalty = float(state.cfg.partner_repeat_penalty or 0.0)
    counts = defaultdict(int)

    # If no penalty is configured, keep the old deterministic behavior.
    if penalty <= 0.0:
        out: List[DealProposal] = []
        for p in proposals:
            partner = p.seller_id if partner_side == 'seller' else p.buyer_id
            if counts[partner] >= cap:
                continue
            out.append(p)
            counts[partner] += 1
            if len(out) >= max_results_i:
                break
        try:
            state.stats.partner_counts.clear()
            state.stats.partner_counts.update(counts)
        except Exception:
            pass
        return out

    # Greedy selection with diversity penalty (bounded by max_results).
    remaining = list(proposals)
    out: List[DealProposal] = []
    while remaining and len(out) < max_results_i:
        best_i = -1
        best_adj = None
        best_raw = None
        for i, p in enumerate(remaining):
            partner = p.seller_id if partner_side == 'seller' else p.buyer_id
            if counts[partner] >= cap:
                continue
            adj = float(p.score) - penalty * float(max(0, counts[partner]))
            if best_adj is None or adj > best_adj or (adj == best_adj and (best_raw is None or p.score > best_raw)):
                best_adj = adj
                best_raw = float(p.score)
                best_i = i
        if best_i < 0:
            break
        chosen = remaining.pop(best_i)
        partner = chosen.seller_id if partner_side == 'seller' else chosen.buyer_id
        out.append(chosen)
        counts[partner] += 1

    try:
        state.stats.partner_counts.clear()
        state.stats.partner_counts.update(counts)
    except Exception:
        pass

    return out


# =============================================================================
# Dedupe fingerprint
# =============================================================================
def _deal_fingerprint_2team(deal: Deal) -> str:
    """
    Canonical fingerprint that ignores meta and treats 2-team deals with to_team=None as standard.
    """
    try:
        d = Deal(teams=list(deal.teams), legs=dict(deal.legs), meta={})
        cd = canonicalize_deal(d)
        payload = serialize_deal(cd)
        payload.pop("meta", None)
        s = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        h = hashlib.blake2b(s.encode("utf-8"), digest_size=16).hexdigest()
        return h
    except Exception:
        return str(id(deal))
