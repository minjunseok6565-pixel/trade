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
        }


# =============================================================================
# Internal helper types
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

    # dedupe across generated deals
    seen_fingerprints: Set[str] = field(default_factory=set)


# =============================================================================
# DealGenerator
# =============================================================================
class DealGenerator:
    def __init__(self, config: Optional[DealGeneratorConfig] = None) -> None:
        self.config = config or DealGeneratorConfig()
        self.last_stats: Optional[DealGenerationStats] = None

    def generate_for_team(
        self,
        team_id: str,
        tick_ctx: TradeGenerationTickContext,
        *,
        max_results: int = 10,
        allow_locked_by_deal_id: Optional[str] = None,
        rng_seed: Optional[int] = None,
    ) -> List[DealProposal]:
        """Generate candidate deals for a given team (2-team deals only)."""
        tid = _canon_team_id(team_id)
        ts = tick_ctx.get_team_situation(tid)

        # Early exit: market throttling / stand pat
        if getattr(ts, "constraints", None) is not None:
            if bool(getattr(ts.constraints, "cooldown_active", False)):
                return []
        if str(getattr(ts, "trade_posture", "STAND_PAT")).upper() == "STAND_PAT" and float(getattr(ts, "urgency", 0.0) or 0.0) < self.config.stand_pat_min_urgency:
            return []

        # Ensure asset catalog exists on tick context
        if tick_ctx.asset_catalog is None:
            tick_ctx.asset_catalog = build_trade_asset_catalog(tick_ctx=tick_ctx)  # type: ignore[arg-type]
        catalog = tick_ctx.asset_catalog
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

        # Select targets (IncomingPlayerRef list)
        targets = _select_targets(state, buyer_id=tid, budgets=budgets)
        targets = targets[: max(0, budgets.max_targets)]

        results: List[DealProposal] = []
        per_target_best: DefaultDict[str, List[DealProposal]] = defaultdict(list)

        for ref in targets:
            seller_id = _canon_team_id(ref.from_team)
            if not seller_id or seller_id == tid:
                continue

            # partner spam limit (hard)
            if stats.partner_counts[seller_id] >= self.config.max_partner_repeats:
                continue

            # build deal skeletons for this target
            skeletons = _build_offer_skeletons(state, buyer_id=tid, target_ref=ref)
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
                proposal = _evaluate_and_score(state, deal, buyer_id=tid, seller_id=seller_id)
                if proposal is None:
                    continue

                # optional sweetener loop for near-miss
                proposals_to_add = [proposal]
                if self.config.enable_sweeteners:
                    proposals_to_add = _sweetener_loop(state, proposal, budgets=budgets)

                for p in proposals_to_add:
                    per_target_best[ref.player_id].append(p)

                # maintain per-target beam
                if per_target_best[ref.player_id]:
                    per_target_best[ref.player_id].sort(key=lambda x: x.score, reverse=True)
                    per_target_best[ref.player_id] = per_target_best[ref.player_id][: budgets.beam_width]

            # partner count increments only if we produced at least one valid proposal
            if per_target_best.get(ref.player_id):
                stats.partner_counts[seller_id] += 1

            # early stop if enough results and budgets tight
            if stats.validations >= budgets.max_validations or stats.evaluations >= budgets.max_evaluations:
                break

        # flatten beams
        for lst in per_target_best.values():
            results.extend(lst)

        # global sort + trim
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


# =============================================================================
# Target selection
# =============================================================================
def _select_targets(state: _GenState, *, buyer_id: str, budgets: _Budgets) -> List[IncomingPlayerRef]:
    cfg = state.cfg
    tick_ctx = state.tick_ctx
    catalog = state.catalog

    buyer_dc = tick_ctx.get_decision_context(buyer_id)
    need_map = getattr(buyer_dc, "need_map", {}) or {}
    if not isinstance(need_map, dict):
        need_map = {}

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

    # resolve full target candidate
    target = seller_out.players.get(target_ref.player_id)
    if target is None:
        return []

    # seller willingness prefilter: avoid CORE unless seller posture is SELL-ish
    seller_ts = tick_ctx.get_team_situation(seller_id)
    seller_posture = str(getattr(seller_ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()
    if "CORE" in (target.buckets or ()):
        if seller_posture not in ("SELL", "SOFT_SELL"):
            return []
        # even in SELL, core sales should be rare; keep but low probability
        if rng.random() > 0.10:
            return []

    # lock / return-ban / eligibility prefilter using catalog snapshot
    if _is_locked(target.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
        state.stats.pruned_locked += 1
        return []
    if target_ref.from_team and buyer_id in set(target.return_ban_teams or ()):
        state.stats.pruned_ineligible += 1
        return []
    if target.recent_signing_banned_until or target.aggregation_banned_until:
        # can't trade out right now
        state.stats.pruned_ineligible += 1
        return []

    # Build candidate sets for buyer outgoing
    buyer_players = _collect_buyer_player_candidates(state, buyer_out)
    filler = buyer_players["filler"]
    match = buyer_players["match"]
    young = buyer_players["young"]

    skeletons: List[_DealSpec] = []

    # archetype 0: straight player-for-player (simple)
    for p in _sample_near_salary(match, target.salary_m, rng=rng, k=2):
        sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
        sk.seller_players_out = [target.player_id]
        sk.buyer_players_out = [p.player_id]
        sk.tags.append("archetype:p4p")
        sk.tags.append(f"need:{target_ref.tag}")
        skeletons.append(sk)

    # archetype 1: picks-only (only if buyer can absorb via cap room)
    if cfg.enable_picks_only and _buyer_can_absorb_target(tick_ctx, buyer_id, target.salary_m):
        for picks, swaps, tag in _picks_packages(state, buyer_out, max_packages=3):
            sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
            sk.seller_players_out = [target.player_id]
            sk.buyer_picks_out = list(picks)
            sk.buyer_swaps_out = list(swaps)
            sk.tags.append("archetype:picks_only")
            sk.tags.append(tag)
            sk.tags.append(f"need:{target_ref.tag}")
            skeletons.append(sk)

    # archetype 2: young + pick (rebuild sellers prefer)
    if young:
        for p in young[:2]:
            for picks, swaps, tag in _picks_packages(state, buyer_out, max_packages=2, prefer_second=True):
                sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
                sk.seller_players_out = [target.player_id]
                sk.buyer_players_out = [p.player_id]
                sk.buyer_picks_out = list(picks)
                sk.buyer_swaps_out = list(swaps)
                sk.tags.append("archetype:young+pick")
                sk.tags.append(tag)
                sk.tags.append(f"need:{target_ref.tag}")
                skeletons.append(sk)

    # archetype 3: salary match focus (filler + small sweetener)
    for p in _sample_near_salary(filler, target.salary_m, rng=rng, k=2):
        sk = _DealSpec(buyer_id=buyer_id, seller_id=seller_id)
        sk.seller_players_out = [target.player_id]
        sk.buyer_players_out = [p.player_id]
        sk.tags.append("archetype:salary_match")
        sk.tags.append(f"need:{target_ref.tag}")
        skeletons.append(sk)

    # archetype 4: consolidate 2-for-1 (bounded)
    if cfg.enable_consolidate_2for1:
        # avoid creating multi-player outgoing if we might hit aggregation solo restrictions frequently
        cons = buyer_players["consolidate"]
        if cons and filler:
            top_a = cons[:2]
            top_b = filler[:3]
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
                    skeletons.append(sk)

    # shuffle + keep only a small bounded set per target
    rng.shuffle(skeletons)
    return skeletons[: max(5, min(14, 2 * state.cfg.beam_width))]


def _buyer_can_absorb_target(tick_ctx: TradeGenerationTickContext, buyer_id: str, target_salary_m: float) -> bool:
    ts = tick_ctx.get_team_situation(buyer_id)
    cap_space = 0.0
    try:
        cap_space = float(getattr(getattr(ts, "constraints", None), "cap_space", 0.0) or 0.0)
    except Exception:
        cap_space = 0.0
    # cap_space is in dollars, target_salary_m is in millions
    return cap_space >= float(target_salary_m) * 1_000_000.0 * 1.02


def _collect_buyer_player_candidates(state: _GenState, buyer_out: TeamOutgoingCatalog) -> Dict[str, List[PlayerTradeCandidate]]:
    """Bucket buyer outgoing players into candidate sets for archetypes."""
    cfg = state.cfg
    tick_ctx = state.tick_ctx
    buyer_id = buyer_out.team_id
    buyer_ts = tick_ctx.get_team_situation(buyer_id)
    posture = str(getattr(buyer_ts, "trade_posture", "STAND_PAT") or "STAND_PAT").upper()

    # gather players excluding banned/locked
    all_players: List[PlayerTradeCandidate] = []
    for pid, cand in buyer_out.players.items():
        if pid in state.banned_players[buyer_id]:
            continue
        if _is_locked(cand.lock, allow_locked_by_deal_id=state.allow_locked_by_deal_id):
            continue
        if cand.recent_signing_banned_until:
            continue
        # keep return bans irrelevant here (outgoing)
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
    match.sort(key=lambda c: (abs(float(getattr(c.salary_m, 0.0) or 0.0) - 10.0), -float(getattr(c.market, "total", 0.0) or 0.0), c.player_id))
    consolidate.sort(key=lambda c: (-float(getattr(c.market, "total", 0.0) or 0.0), -float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))
    young.sort(key=lambda c: (-float(getattr(c.market, "total", 0.0) or 0.0), float(getattr(c.salary_m, 0.0) or 0.0), c.player_id))

    # In BUY posture, be less willing to ship out high-value consolidate pieces
    if posture in ("AGGRESSIVE_BUY", "SOFT_BUY"):
        consolidate = consolidate[:4]

    return {"filler": filler[:12], "match": match[:10], "young": young[:6], "consolidate": consolidate[:8]}


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


def _picks_packages(state: _GenState, buyer_out: TeamOutgoingCatalog, *, max_packages: int, prefer_second: bool = False) -> List[Tuple[Tuple[str, ...], Tuple[str, ...], str]]:
    """Build a few pick/swap packages for the buyer (ordered cheap->expensive)."""
    picks_second = [pid for pid in buyer_out.pick_ids_by_bucket.get("SECOND", ()) if pid not in state.banned_picks[buyer_out.team_id]]
    picks_first_safe = [pid for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SAFE", ()) if pid not in state.banned_picks[buyer_out.team_id]]
    picks_first_sens = [pid for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SENSITIVE", ()) if pid not in state.banned_picks[buyer_out.team_id]]
    swaps = [sid for sid in buyer_out.swap_ids if sid not in state.banned_swaps[buyer_out.team_id]]

    out: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
    # simplest: 2RP
    if picks_second:
        out.append(((picks_second[0],), tuple(), "sweetener:2RP"))
    if len(picks_second) >= 2:
        out.append(((picks_second[0], picks_second[1]), tuple(), "sweetener:2RPx2"))
    # add swap
    if swaps:
        out.append((tuple(), (swaps[0],), "sweetener:swap"))
    # 1RP safe
    if picks_first_safe:
        out.append(((picks_first_safe[0],), tuple(), "sweetener:1RP_SAFE"))
    # sensitive last
    if picks_first_sens:
        out.append(((picks_first_sens[0],), tuple(), "sweetener:1RP_SENSITIVE"))

    if prefer_second:
        # stable order emphasizing seconds first
        pass
    # keep bounded
    return out[: max_packages]


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

            if rule == "salary_matching":
                if method == "second_apron_one_for_one":
                    if not _repair_second_apron_one_for_one(state, current):
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

    # In 2-team deal, roster-limit violation happens to the receiver of too many players.
    # Easiest repair: remove the least important outgoing player from the opposite sender
    # (i.e., reduce incoming players for this team).
    # Since our deals mostly send 1 player from seller, this is rare; handle generally.
    buyer_id = spec.buyer_id
    seller_id = spec.seller_id

    # Determine which side is sending multiple players to team_id
    if team_id == buyer_id:
        # buyer receives seller players; trim seller outgoing players beyond 1 (keep target)
        if len(spec.seller_players_out) > 1:
            spec.seller_players_out = spec.seller_players_out[:1]
            spec.tags.append("repair:roster_trim_seller")
            return True
    if team_id == seller_id:
        if len(spec.buyer_players_out) > 1:
            spec.buyer_players_out = spec.buyer_players_out[:1]
            spec.tags.append("repair:roster_trim_buyer")
            return True
    return False


def _repair_second_apron_one_for_one(state: _GenState, spec: _DealSpec) -> bool:
    # Enforce 1 outgoing player per team maximum.
    # Keep the "main" players (seller's target, buyer's best match if any).
    changed = False
    if len(spec.seller_players_out) > 1:
        spec.seller_players_out = spec.seller_players_out[:1]
        changed = True
    if len(spec.buyer_players_out) > 1:
        spec.buyer_players_out = spec.buyer_players_out[:1]
        changed = True
    if changed:
        spec.tags.append("repair:second_apron_1for1")
    return changed


def _repair_salary_matching(state: _GenState, spec: _DealSpec, details: Dict[str, Any]) -> bool:
    """Minimal salary-match repair: add/trim filler depending on failing team."""
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

    # If buyer fails (incoming > allowed_in), add a filler player to buyer outgoing.
    if team_id == buyer_id:
        # cannot add if any outgoing player is solo-only and we'd create 2+
        if any(buyer_out.players.get(pid, None) and buyer_out.players[pid].aggregation_solo_only for pid in spec.buyer_players_out):
            return False

        filler_cands = _collect_buyer_player_candidates(state, buyer_out)["filler"]
        used = set(spec.buyer_players_out)
        for c in filler_cands:
            if c.player_id in used:
                continue
            if c.aggregation_solo_only:
                continue
            spec.buyer_players_out.append(c.player_id)
            spec.tags.append("repair:add_filler_buyer")
            return True

        return False

    # If seller fails, buyer outgoing salary is too high relative to seller outgoing.
    if team_id == seller_id:
        # Trim buyer filler players (keep first), or swap to cheaper candidate.
        if len(spec.buyer_players_out) >= 2:
            spec.buyer_players_out = spec.buyer_players_out[:1]
            spec.tags.append("repair:trim_filler_buyer")
            return True
        # If single player but too expensive, try swap to cheaper match candidate
        if len(spec.buyer_players_out) == 1:
            current_pid = spec.buyer_players_out[0]
            current_c = buyer_out.players.get(current_pid)
            current_salary = float(getattr(current_c, "salary_m", 0.0) or 0.0) if current_c else 0.0
            match_cands = _collect_buyer_player_candidates(state, buyer_out)["match"]
            cheaper = [c for c in match_cands if float(getattr(c, "salary_m", 0.0) or 0.0) <= current_salary - 0.25]
            if cheaper:
                spec.buyer_players_out[0] = cheaper[0].player_id
                spec.tags.append("repair:swap_cheaper_buyer")
                return True
        return False

    return False


def _repair_pick_rules(state: _GenState, spec: _DealSpec, details: Dict[str, Any]) -> bool:
    """Downgrade/remove picks to satisfy Stepien or pick horizon."""
    # Simple strategy: remove the last-added pick/swap from the side indicated.
    team_id = _canon_team_id(details.get("team_id") or "")
    if not team_id:
        # If missing, assume buyer side (most common)
        team_id = spec.buyer_id

    if team_id == spec.buyer_id:
        if spec.buyer_picks_out:
            removed = spec.buyer_picks_out.pop()
            state.banned_picks[spec.buyer_id].add(str(removed))
            spec.tags.append("repair:drop_pick")
            return True
        if spec.buyer_swaps_out:
            removed = spec.buyer_swaps_out.pop()
            state.banned_swaps[spec.buyer_id].add(str(removed))
            spec.tags.append("repair:drop_swap")
            return True
    if team_id == spec.seller_id:
        if spec.seller_picks_out:
            removed = spec.seller_picks_out.pop()
            state.banned_picks[spec.seller_id].add(str(removed))
            spec.tags.append("repair:drop_pick_seller")
            return True
        if spec.seller_swaps_out:
            removed = spec.seller_swaps_out.pop()
            state.banned_swaps[spec.seller_id].add(str(removed))
            spec.tags.append("repair:drop_swap_seller")
            return True

    return False


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
        # eligibility prefilter
        if cand.recent_signing_banned_until or cand.aggregation_banned_until:
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
def _evaluate_and_score(state: _GenState, deal: Deal, *, buyer_id: str, seller_id: str) -> Optional[DealProposal]:
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

    # partner spam penalty: discourage repetitive same opponent
    repeats = max(0, int(state.stats.partner_counts.get(seller_id, 0)))
    if repeats > 0:
        score -= cfg.partner_repeat_penalty * float(repeats)

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
    def _n_players(side):
        n = 0
        for tv in getattr(side, "incoming", ()):
            if str(getattr(tv, "kind", "")).lower() == "player":
                n += 1
        for tv in getattr(side, "outgoing", ()):
            if str(getattr(tv, "kind", "")).lower() == "player":
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
def _sweetener_loop(state: _GenState, proposal: DealProposal, *, budgets: _Budgets) -> List[DealProposal]:
    cfg = state.cfg
    buyer_id = proposal.buyer_id
    seller_id = proposal.seller_id

    # compute seller margin
    seller_margin = float(getattr(proposal.seller_eval, "net_surplus", 0.0) or 0.0) - float(getattr(proposal.seller_decision, "required_surplus", 0.0) or 0.0)
    if seller_margin >= 0:
        return [proposal]
    if seller_margin < -float(cfg.near_miss_margin_max):
        return [proposal]

    # Only try if seller rejects (or counters). If they accept, no need.
    if getattr(proposal.seller_decision, "verdict", None) == DealVerdict.ACCEPT:
        return [proposal]

    buyer_out = state.catalog.outgoing_by_team.get(buyer_id)
    if buyer_out is None:
        return [proposal]

    stepien = state.catalog.stepien
    new_props: List[DealProposal] = [proposal]
    current_spec = _deal_to_spec_guess(proposal.deal, buyer_id=buyer_id, seller_id=seller_id)
    if current_spec is None:
        return [proposal]

    used_picks = set(current_spec.buyer_picks_out)
    used_swaps = set(current_spec.buyer_swaps_out)

    for token in cfg.sweetener_order[: max(0, cfg.max_sweeteners)]:
        if state.stats.validations >= budgets.max_validations or state.stats.evaluations >= budgets.max_evaluations:
            break

        # choose next asset
        added = False
        if token == "SECOND":
            for pid in buyer_out.pick_ids_by_bucket.get("SECOND", ()):
                if pid in used_picks or pid in state.banned_picks[buyer_id]:
                    continue
                if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                    continue
                current_spec.buyer_picks_out.append(pid)
                used_picks.add(pid)
                current_spec.tags.append("sweetener:2RP")
                added = True
                break

        elif token == "FIRST_SAFE":
            for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SAFE", ()):
                if pid in used_picks or pid in state.banned_picks[buyer_id]:
                    continue
                if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                    continue
                current_spec.buyer_picks_out.append(pid)
                used_picks.add(pid)
                current_spec.tags.append("sweetener:1RP_SAFE")
                added = True
                break

        elif token == "FIRST_SENSITIVE":
            for pid in buyer_out.pick_ids_by_bucket.get("FIRST_SENSITIVE", ()):
                if pid in used_picks or pid in state.banned_picks[buyer_id]:
                    continue
                if not _stepien_ok_after(stepien, buyer_id, outgoing_pick_ids=set(used_picks) | {pid}):
                    continue
                current_spec.buyer_picks_out.append(pid)
                used_picks.add(pid)
                current_spec.tags.append("sweetener:1RP_SENSITIVE")
                added = True
                break

        elif token == "SWAP":
            for sid in buyer_out.swap_ids:
                if sid in used_swaps or sid in state.banned_swaps[buyer_id]:
                    continue
                current_spec.buyer_swaps_out.append(sid)
                used_swaps.add(sid)
                current_spec.tags.append("sweetener:swap")
                added = True
                break

        if not added:
            continue

        # validate + evaluate new deal
        deal2 = _repair_until_valid(state, current_spec, budgets=budgets)
        if deal2 is None:
            continue
        fp = _deal_fingerprint_2team(deal2)
        if fp in state.seen_fingerprints:
            state.stats.pruned_duplicate += 1
            continue
        state.seen_fingerprints.add(fp)

        p2 = _evaluate_and_score(state, deal2, buyer_id=buyer_id, seller_id=seller_id)
        if p2 is None:
            continue
        new_props.append(p2)

        # stop if seller accepted
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


